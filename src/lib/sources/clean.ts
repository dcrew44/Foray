/**
 * HTML to readable text, minus the boilerplate nobody reads.
 *
 * Two rules govern everything here, both from DESIGN.md:
 *
 * 1. `description_raw` is never touched. This produces a separate, disposable
 *    `description_text`; if the cleaning is wrong it can be regenerated.
 * 2. Boilerplate is removed only by known patterns, never by summarising. A
 *    model asked to "trim the fluff" will eventually trim a requirement, and a
 *    posting is the evidence a match is scored against.
 *
 * Boilerplate is also only removed from the tail. Every one of these patterns
 * can legitimately appear in the body of a posting — a diversity-focused role
 * really does discuss equal opportunity in its responsibilities — so position
 * is part of the evidence, not just the wording.
 */

/** Only the last 30% of a posting is eligible for boilerplate removal. */
const TAIL_FRACTION = 0.3;

/**
 * Openers of blocks that are legal or administrative, not part of the job.
 *
 * Each is matched at the start of a paragraph in the tail. Deliberately
 * conservative: a missed block costs a few tokens, a wrongly removed one costs
 * a requirement.
 */
const BOILERPLATE_OPENERS: Array<{ label: string; pattern: RegExp }> = [
  {
    label: "equal opportunity",
    pattern:
      /^(?:[\w\s.,'&-]{0,60}\bis\s+an\s+)?equal\s+(?:employment\s+)?opportunity\b/i,
  },
  {
    label: "equal opportunity (we are)",
    pattern: /^we\s+are\s+an\s+equal\s+(?:employment\s+)?opportunity\b/i,
  },
  {
    label: "non-discrimination",
    pattern:
      /^(?:all\s+qualified\s+applicants|we\s+do\s+not\s+discriminate|qualified\s+applicants\s+will\s+receive)\b/i,
  },
  {
    label: "E-Verify",
    pattern: /^(?:e-verify\b|this\s+employer\s+participates\s+in\s+e-verify)/i,
  },
  {
    label: "pay transparency",
    pattern:
      /^(?:pay\s+transparency|the\s+(?:pay|salary)\s+range\s+(?:listed|shown|provided)|compensation\s+range\s+(?:listed|shown))\b/i,
  },
  {
    label: "accommodation",
    pattern:
      /^(?:if\s+you\s+(?:need|require)\s+(?:an?\s+)?(?:reasonable\s+)?accommodation|we\s+(?:are\s+committed\s+to\s+)?provid(?:e|ing)\s+reasonable\s+accommodation)/i,
  },
  {
    label: "fair chance / criminal history",
    pattern:
      /^(?:pursuant\s+to\s+the\s+(?:san\s+francisco|los\s+angeles)|qualified\s+applicants\s+with\s+(?:arrest|criminal))/i,
  },
  {
    label: "agency notice",
    pattern:
      /^(?:(?:no\s+agencies|agencies\s+only)|(?:we\s+do\s+not\s+accept\s+(?:unsolicited\s+)?(?:resumes|agency)))/i,
  },
  {
    label: "privacy notice",
    pattern:
      /^(?:(?:for\s+)?(?:california|eu|uk)\s+(?:applicants|residents)|by\s+(?:applying|submitting)[\w\s,]{0,40}\byou\s+(?:consent|acknowledge))/i,
  },
];

const BLOCK_TAGS =
  /<\/?(?:p|div|br|li|ul|ol|h[1-6]|tr|table|section|article|header|footer|blockquote)\b[^>]*>/gi;

/** Entities that actually show up in ATS payloads. */
const ENTITIES: Record<string, string> = {
  amp: "&",
  lt: "<",
  gt: ">",
  quot: '"',
  apos: "'",
  nbsp: " ",
  ndash: "–",
  mdash: "—",
  rsquo: "’",
  lsquo: "‘",
  ldquo: "“",
  rdquo: "”",
  hellip: "…",
  bull: "•",
  middot: "·",
};

export function decodeEntities(input: string): string {
  return input.replace(/&(#x?[0-9a-f]+|[a-z]+);/gi, (match, entity: string) => {
    if (entity.startsWith("#x") || entity.startsWith("#X")) {
      const code = Number.parseInt(entity.slice(2), 16);
      return Number.isFinite(code) ? String.fromCodePoint(code) : match;
    }
    if (entity.startsWith("#")) {
      const code = Number.parseInt(entity.slice(1), 10);
      return Number.isFinite(code) ? String.fromCodePoint(code) : match;
    }
    return ENTITIES[entity.toLowerCase()] ?? match;
  });
}

/** HTML to plain text, preserving paragraph and list structure. */
export function htmlToText(html: string): string {
  return decodeEntities(
    html
      // Script and style content is never prose.
      .replace(/<(script|style)\b[^>]*>[\s\S]*?<\/\1>/gi, "")
      // List items become bullets so the structure survives as text.
      .replace(/<li\b[^>]*>/gi, "\n• ")
      .replace(BLOCK_TAGS, "\n")
      .replace(/<[^>]+>/g, ""),
  )
    .replace(/\r\n?/g, "\n")
    .replace(/[ \t ]+/g, " ")
    .replace(/ *\n */g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

export type CleanResult = {
  text: string;
  /** Which boilerplate blocks were removed, for auditing the patterns. */
  removed: string[];
};

/**
 * Produces `description_text` from a raw description.
 *
 * Accepts HTML or plain text — careers-page extraction (Step 14) and the ATS
 * APIs disagree about which they hand over, and sniffing is more robust than
 * asking every caller to declare it.
 */
export function cleanDescription(raw: string | null | undefined): CleanResult {
  if (!raw) return { text: "", removed: [] };

  // Greenhouse entity-escapes its HTML, so tags arrive as `&lt;p&gt;`. Undoing
  // that here rather than in the adapter means any source that does the same is
  // handled without a second special case.
  const unescaped = /&lt;\/?[a-z]/i.test(raw) ? decodeEntities(raw) : raw;

  const looksLikeHtml = /<\/?[a-z][\s\S]*>/i.test(unescaped);
  const text = looksLikeHtml
    ? htmlToText(unescaped)
    : decodeEntities(unescaped).trim();

  const paragraphs = text.split(/\n{2,}/);
  if (paragraphs.length < 2) return { text, removed: [] };

  // Where the tail begins, measured in characters rather than paragraphs so a
  // posting that ends in many short legal blocks is handled the same as one
  // that ends in a single long one.
  const total = text.length;
  const cutoff = total * (1 - TAIL_FRACTION);

  const removed: string[] = [];
  let consumed = 0;

  const kept = paragraphs.filter((paragraph) => {
    const start = consumed;
    consumed += paragraph.length + 2;

    if (start < cutoff) return true;

    const opener = paragraph.replace(/^[\s•\-*]+/, "");
    const match = BOILERPLATE_OPENERS.find((rule) =>
      rule.pattern.test(opener),
    );
    if (match) {
      removed.push(match.label);
      return false;
    }
    return true;
  });

  return { text: kept.join("\n\n").trim(), removed };
}
