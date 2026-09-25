import { z } from "zod";

import type { LlmContext } from "@/lib/llm/context";
import { generateStructured } from "@/lib/llm/structured";

import { factIds, type Resume } from "./schema";
import type { TailorChange, TailorSection } from "./tailor";
import { resumeToText } from "./text";

/**
 * The truthfulness check on a tailored resume.
 *
 * A tailored resume that invents a skill, an employer or a number is the worst
 * thing this product can produce: the user sends it under their own name. So
 * nothing the tailor writes is trusted. Every line is checked against the base
 * resume in two passes:
 *
 * 1. Deterministic rules, which cannot be talked round: items must be base
 *    items, names/titles/dates must be verbatim, skills must be base skills,
 *    and every number, link and technology-looking term in a rewritten line
 *    must already appear in the base item it sits under.
 * 2. One cheap model call over the rewritten lines that passed the rules,
 *    asking whether the original item actually supports each one — the check
 *    rules cannot do ("contributed to" becoming "led").
 *
 * Anything flagged gets one deterministic revision: the line goes back to the
 * original bullet it most resembles, a changed field goes back to the base
 * value, an unknown skill is dropped. Then the rules run again, and whatever
 * still fails stays flagged for the user to see.
 */

/** A problem found in a tailored resume, in the shape the UI shows. */
export type UnsupportedClaim = {
  section: string;
  item_id: string | null;
  text: string;
  reason: string;
  source: "rule" | "model";
};

/** What the automatic revision did about one problem. */
export type Revision = UnsupportedClaim & {
  action: "replaced" | "dropped" | "reverted";
  /** The base text or value put back, when something was. */
  replacement: string | null;
};

/** Stored in `resumes.verification`. */
export type Verification = {
  /** True when nothing on the final resume is unsupported. */
  passed: boolean;
  checkedAt: string;
  /** What is still unsupported on the saved resume — after revision. */
  unsupported: UnsupportedClaim[];
  /** Whether anything was changed to make the resume truthful. */
  revised: boolean;
  /** Lines checked: bullets, summary sentences, descriptions and skills. */
  checked: number;
  /** Rewritten lines the model judged. */
  modelChecked: number;
  /** What was reverted or dropped, and why — the audit trail of the fix. */
  revisions: Revision[];
  /** Set when the model check could not run; every rewrite was then reverted. */
  modelError: string | null;
  /** The base resume version this was checked against. */
  baseResumeId: string | null;
  baseVersion: number | null;
};

type ItemSection = "work" | "education" | "projects";

/** Where a problem is, precisely enough for the revision to fix it. */
export type Target =
  | { kind: "basics"; field: "name" | "email" | "phone" | "location" | "headline" | "links" }
  | { kind: "item"; section: ItemSection; itemId: string }
  | { kind: "field"; section: ItemSection; itemId: string; field: string }
  | { kind: "skill"; index: number }
  | { kind: "text"; key: string };

export type Finding = UnsupportedClaim & { target: Target };

// --- Claims -----------------------------------------------------------------

type ClaimField = "highlight" | "summary" | "description" | "sentence";

/** One line of tailored text that asserts something about the candidate. */
export type Claim = {
  key: string;
  section: TailorSection;
  itemId: string | null;
  field: ClaimField;
  index: number;
  text: string;
  /** Identical to base text in the same place: true by construction. */
  verbatim: boolean;
};

/** Case, whitespace, quote and dash differences do not make a line new. */
export function normalizeText(value: string): string {
  return value
    .normalize("NFKC")
    .toLowerCase()
    .replace(/[‘’]/g, "'")
    .replace(/[“”]/g, '"')
    .replace(/[‐‑‒–—]/g, "-")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/[.;,]+$/, "");
}

/**
 * Sentences, split conservatively: only after a lowercase word, digit or
 * closing bracket, so "B.S. in", "U.S. Army" and "e.g" do not split. A missed
 * split merges two sentences into one claim, which is safe; a wrong split
 * could leave half a sentence behind when the other half is dropped.
 */
export function splitSentences(text: string): string[] {
  return text
    .split(/(?<=[a-z0-9%)"'’”][.!?])\s+(?=[A-Z"“(])/)
    .map((sentence) => sentence.trim())
    .filter((sentence) => sentence.length > 0);
}

export function collectClaims(base: Resume, tailored: Resume): Claim[] {
  const claims: Claim[] = [];
  const same = (a: string | null | undefined, b: string) =>
    Boolean(a?.trim()) && normalizeText(a!) === normalizeText(b);

  if (tailored.summary?.trim()) {
    const baseSentences = new Set(splitSentences(base.summary ?? "").map(normalizeText));
    const whole = same(base.summary, tailored.summary);
    splitSentences(tailored.summary).forEach((text, index) =>
      claims.push({
        key: `summary:sentence:${index}`,
        section: "summary",
        itemId: null,
        field: "sentence",
        index,
        text,
        verbatim: whole || baseSentences.has(normalizeText(text)),
      }),
    );
  }

  const bulletsOf = (
    section: ItemSection,
    itemId: string,
    highlights: string[],
    original: string[] | undefined,
  ) => {
    const known = new Set((original ?? []).map(normalizeText));
    highlights.forEach((text, index) =>
      claims.push({
        key: `${section}:${itemId}:highlight:${index}`,
        section,
        itemId,
        field: "highlight",
        index,
        text,
        verbatim: known.has(normalizeText(text)),
      }),
    );
  };

  const baseWork = new Map(base.work.map((item) => [item.id, item]));
  for (const item of tailored.work) {
    const original = baseWork.get(item.id);
    if (item.summary?.trim()) {
      claims.push({
        key: `work:${item.id}:summary:0`,
        section: "work",
        itemId: item.id,
        field: "summary",
        index: 0,
        text: item.summary,
        verbatim: same(original?.summary, item.summary),
      });
    }
    bulletsOf("work", item.id, item.highlights, original?.highlights);
  }

  const baseProjects = new Map(base.projects.map((item) => [item.id, item]));
  for (const item of tailored.projects) {
    const original = baseProjects.get(item.id);
    if (item.description?.trim()) {
      claims.push({
        key: `projects:${item.id}:description:0`,
        section: "projects",
        itemId: item.id,
        field: "description",
        index: 0,
        text: item.description,
        verbatim: same(original?.description, item.description),
      });
    }
    bulletsOf("projects", item.id, item.highlights, original?.highlights);
  }

  const baseEducation = new Map(base.education.map((item) => [item.id, item]));
  for (const item of tailored.education) {
    bulletsOf("education", item.id, item.highlights, baseEducation.get(item.id)?.highlights);
  }

  return claims;
}

// --- Numbers, links and terms ------------------------------------------------

const URL_PATTERN =
  /\bhttps?:\/\/[^\s)<>"']+|\b(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|org|net|io|dev|ai|co|app|edu|gov|me|xyz|tech|so|sh|gg|us|uk|ca|de|fr|in|info|biz|tv|cloud|site|page|blog|design|codes)\b(?:\/[^\s)<>"',;]*)?/gi;
const EMAIL_PATTERN = /[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}/gi;

function stripLinks(text: string): string {
  return text.replace(EMAIL_PATTERN, " ").replace(URL_PATTERN, " ");
}

function normalizeLink(value: string): string {
  return value
    .toLowerCase()
    .replace(/^https?:\/\//, "")
    .replace(/^www\./, "")
    .replace(/[/.,;:)]+$/, "");
}

/** URLs and email addresses mentioned in a line, normalized for comparison. */
export function extractLinks(text: string): string[] {
  const emails = text.match(EMAIL_PATTERN) ?? [];
  const urls = text.replace(EMAIL_PATTERN, " ").match(URL_PATTERN) ?? [];
  return [...new Set([...emails, ...urls].map(normalizeLink))];
}

const TENS: Record<string, number> = {
  twenty: 20,
  thirty: 30,
  forty: 40,
  fifty: 50,
  sixty: 60,
  seventy: 70,
  eighty: 80,
  ninety: 90,
};

const UNITS: Record<string, number> = {
  one: 1,
  two: 2,
  three: 3,
  four: 4,
  five: 5,
  six: 6,
  seven: 7,
  eight: 8,
  nine: 9,
};

/**
 * Number words that state a quantity. "one" is left out: it is far more often
 * "one of" than a metric, and flagging it would revert harmless rewrites.
 */
const NUMBER_WORDS: Record<string, number> = {
  ...Object.fromEntries(Object.entries(UNITS).filter(([word]) => word !== "one")),
  ten: 10,
  eleven: 11,
  twelve: 12,
  thirteen: 13,
  fourteen: 14,
  fifteen: 15,
  sixteen: 16,
  seventeen: 17,
  eighteen: 18,
  nineteen: 19,
  ...TENS,
  hundred: 100,
  dozen: 12,
};

const MULTIPLIER_WORDS: Record<string, number> = {
  twice: 2,
  doubled: 2,
  doubling: 2,
  twofold: 2,
  tripled: 3,
  tripling: 3,
  threefold: 3,
  quadrupled: 4,
  fourfold: 4,
  fivefold: 5,
  tenfold: 10,
};

/** A number as written, and every value it could reasonably stand for. */
export type Metric = { raw: string; keys: string[] };

const fmt = (value: number) => String(Number(value.toPrecision(12)));

/**
 * What follows a number decides what it means: "40%" and "40-million-row"
 * share a digit and nothing else. Each suffix maps to comparison keys; an
 * ambiguous one ("26m": minutes or million) yields every reading, and a
 * number passes if any reading appears in the base.
 */
const SUFFIXES: Array<[RegExp, (value: number) => string[]]> = [
  [/^\s?(?:%|percent\b|per\s?cent\b|pct\b)/i, (v) => [`pct:${fmt(v)}`]],
  [/^\s?-?\s?(?:x|×)(?![\p{L}\p{N}])/iu, (v) => [`x:${fmt(v)}`]],
  [/^\s?-?\s?fold\b/i, (v) => [`x:${fmt(v)}`]],
  [/^\s?times\b/i, (v) => [`x:${fmt(v)}`, `n:${fmt(v)}`]],
  [/^\s?-?\s?(?:thousand\b|k(?![\p{L}\p{N}]))/iu, (v) => [`n:${fmt(v * 1e3)}`]],
  [/^\s?-?\s?(?:million\b|mil\b|mn\b)/i, (v) => [`n:${fmt(v * 1e6)}`]],
  [/^\s?-?\s?(?:MM|M)(?![\p{L}\p{N}])/u, (v) => [`n:${fmt(v * 1e6)}`]],
  [/^\s?-?\s?(?:mm|m)(?![\p{L}\p{N}])/u, (v) => [`n:${fmt(v * 1e6)}`, `n:${fmt(v)}`]],
  [/^\s?-?\s?(?:billion\b|bn\b)/i, (v) => [`n:${fmt(v * 1e9)}`]],
  [/^\s?-?\s?B(?![\p{L}\p{N}])/u, (v) => [`n:${fmt(v * 1e9)}`]],
];

export function extractMetrics(text: string): Metric[] {
  const clean = stripLinks(text);
  const metrics: Metric[] = [];

  // Digits. A number glued to a letter ("S3", "EC2", "Q3", "v2") is a name,
  // not a quantity, and is left to the term check.
  const digits = /(?<![\p{L}\p{N}_.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)/gu;
  for (const match of clean.matchAll(digits)) {
    const value = Number(match[1].replace(/,/g, ""));
    if (!Number.isFinite(value)) continue;
    const rest = clean.slice(match.index + match[0].length);
    const suffix = SUFFIXES.find(([pattern]) => pattern.test(rest));
    if (suffix) {
      const [pattern, keys] = suffix;
      metrics.push({ raw: match[0] + rest.match(pattern)![0], keys: keys(value) });
    } else {
      metrics.push({ raw: match[0], keys: [`n:${fmt(value)}`] });
    }
  }

  // Words: "twenty-five", "six", "tripled".
  const tens = Object.keys(TENS).join("|");
  const units = Object.keys(UNITS).join("|");
  const words = Object.keys(NUMBER_WORDS).join("|");
  const multipliers = Object.keys(MULTIPLIER_WORDS).join("|");
  const wordPattern = new RegExp(
    `\\b(?:(${tens})[-\\s](${units})|(${words})|(${multipliers}))\\b`,
    "gi",
  );
  for (const match of clean.matchAll(wordPattern)) {
    if (match[1] && match[2]) {
      const value = TENS[match[1].toLowerCase()] + UNITS[match[2].toLowerCase()];
      metrics.push({ raw: match[0], keys: [`n:${value}`] });
    } else if (match[3]) {
      metrics.push({ raw: match[0], keys: [`n:${NUMBER_WORDS[match[3].toLowerCase()]}`] });
    } else if (match[4]) {
      metrics.push({ raw: match[0], keys: [`x:${MULTIPLIER_WORDS[match[4].toLowerCase()]}`] });
    }
  }

  return metrics;
}

function metricKeys(text: string): Set<string> {
  return new Set(extractMetrics(text).flatMap((metric) => metric.keys));
}

/**
 * Words that name something — a technology, product, company or credential.
 *
 * Mixed case ("TypeScript", "AWS", "gRPC"), inner digits or symbols
 * ("Node.js", "C++", "S3"), or a capital letter in the middle of a sentence
 * ("with Kubernetes"). A capitalised first word is usually a verb ("Built"),
 * so it only counts when it is mixed-case or symbolic.
 */
export function extractTerms(text: string): string[] {
  const clean = stripLinks(text);
  const terms: string[] = [];

  for (const match of clean.matchAll(/[\p{L}\p{N}][\p{L}\p{N}.+#'’/-]*/gu)) {
    const token = match[0].replace(/['’]s$/i, "").replace(/[.\-/'’]+$/, "");
    const initial = isSentenceStart(clean, match.index);

    token.split(/[/-]/).forEach((part, index) => {
      const word = part.replace(/^\.+|\.+$/g, "");
      if (word.length < 2 || /^\d/.test(word) || !/\p{L}/u.test(word)) return;
      const rest = word.slice(1);
      const mixed = /\p{Lu}/u.test(rest);
      const symbolic = /[\d.+#]/.test(rest);
      const capital = /^\p{Lu}/u.test(word) && !(initial && index === 0);
      if (mixed || symbolic || capital) terms.push(word);
    });
  }

  return [...new Set(terms)];
}

function isSentenceStart(text: string, index: number): boolean {
  const before = text.slice(0, index).trimEnd();
  return before.length === 0 || /[.!?:;•]$/.test(before);
}

/** Equivalent spellings, so a rewrite may use the posting's name for a base term. */
const ALIASES: Record<string, string[]> = {
  postgres: ["postgresql"],
  postgresql: ["postgres"],
  k8s: ["kubernetes"],
  kubernetes: ["k8s"],
  js: ["javascript"],
  ts: ["typescript"],
  golang: ["go"],
  node: ["node.js", "nodejs"],
  nodejs: ["node.js", "node"],
  "node.js": ["node", "nodejs"],
  reactjs: ["react"],
  "react.js": ["react"],
  react: ["react.js", "reactjs"],
  next: ["next.js"],
  "next.js": ["next"],
  "vue.js": ["vue"],
  vue: ["vue.js"],
};

function containsWord(haystack: string, needle: string): boolean {
  let from = 0;
  for (;;) {
    const at = haystack.indexOf(needle, from);
    if (at < 0) return false;
    const before = haystack[at - 1];
    const after = haystack[at + needle.length];
    const boundary = (char: string | undefined) => !char || !/[\p{L}\p{N}]/u.test(char);
    if (boundary(before) && boundary(after)) return true;
    from = at + 1;
  }
}

function termIn(term: string, scopeLower: string): boolean {
  const lower = term.toLowerCase();
  const variants = new Set([lower, ...(ALIASES[lower] ?? [])]);
  // Plurals and possessives of a base term: "APIs" for "API".
  if (lower.length > 3 && lower.endsWith("s")) variants.add(lower.slice(0, -1));
  if (lower.length > 4 && lower.endsWith("es")) variants.add(lower.slice(0, -2));
  return [...variants].some((variant) => containsWord(scopeLower, variant));
}

// --- Scopes -------------------------------------------------------------------

function strings(value: unknown): string[] {
  if (typeof value === "string") return [value];
  if (Array.isArray(value)) return value.flatMap(strings);
  if (value && typeof value === "object") {
    return Object.entries(value)
      .filter(([key]) => key !== "id" && key !== "source")
      .flatMap(([, inner]) => strings(inner));
  }
  return [];
}

type Scope = { text: string; lower: string; metrics: Set<string> };

function scope(text: string): Scope {
  return { text, lower: text.toLowerCase(), metrics: metricKeys(text) };
}

/**
 * What a line may draw on. A bullet may only use its own item — moving a
 * number from one job to another is fabrication too. The summary may draw on
 * the whole resume.
 */
function scopes(base: Resume) {
  const items = new Map<string, Scope>();
  for (const item of [...base.work, ...base.education, ...base.projects]) {
    items.set(item.id, scope(strings(item).join("\n")));
  }
  return { items, resume: scope(strings(base).join("\n")) };
}

// --- Deterministic rules --------------------------------------------------------

const STRICT_FIELDS: Record<ItemSection, string[]> = {
  work: ["company", "position", "startDate", "endDate"],
  education: ["institution", "area", "studyType", "startDate", "endDate"],
  projects: ["name", "url", "startDate", "endDate"],
};

/** Fields a tailored resume may leave out, but never change. */
const OMITTABLE_FIELDS: Record<ItemSection, string[]> = {
  work: ["location"],
  education: ["score"],
  projects: [],
};

const FIELD_LABELS: Record<string, string> = {
  company: "company name",
  position: "job title",
  startDate: "start date",
  endDate: "end date",
  location: "location",
  institution: "school",
  area: "field of study",
  studyType: "degree",
  score: "grade",
  name: "project name",
  url: "project link",
};

const SECTION_NOUN: Record<ItemSection, string> = {
  work: "role",
  education: "education entry",
  projects: "project",
};

const value = (input: unknown): string | null =>
  typeof input === "string" && input.trim() ? input.trim() : null;

function listPhrase(items: string[]): string {
  const quoted = items.slice(0, 4);
  if (quoted.length === 1) return quoted[0];
  return `${quoted.slice(0, -1).join(", ")} and ${quoted.at(-1)}`;
}

/**
 * Every check that needs no model. Pure, so it can run before and after the
 * revision and be tested exhaustively.
 */
export function checkRules(base: Resume, tailored: Resume): Finding[] {
  const findings: Finding[] = [];

  // Basics are copied from the base; anything else is a defect upstream.
  for (const field of ["name", "email", "phone", "location", "headline"] as const) {
    if (value(tailored.basics[field]) !== value(base.basics[field])) {
      findings.push({
        section: "basics",
        item_id: null,
        text: value(tailored.basics[field]) ?? "",
        reason: `Your ${field} must match your resume exactly.`,
        source: "rule",
        target: { kind: "basics", field },
      });
    }
  }
  const linkKey = (links: Resume["basics"]["links"]) =>
    JSON.stringify(links.map((link) => [link.label.trim(), link.url.trim()]));
  if (linkKey(tailored.basics.links) !== linkKey(base.basics.links)) {
    findings.push({
      section: "basics",
      item_id: null,
      text: tailored.basics.links.map((link) => link.url).join(", "),
      reason: "Your links must match your resume exactly.",
      source: "rule",
      target: { kind: "basics", field: "links" },
    });
  }

  // Items: each must be a base item, with its facts unchanged.
  const sections: Array<[ItemSection, Array<{ id: string } & Record<string, unknown>>, Array<{ id: string } & Record<string, unknown>>]> = [
    ["work", base.work, tailored.work],
    ["education", base.education, tailored.education],
    ["projects", base.projects, tailored.projects],
  ];

  for (const [section, baseItems, items] of sections) {
    const byId = new Map(baseItems.map((item) => [item.id, item]));
    for (const item of items) {
      const original = byId.get(item.id);
      if (!original) {
        findings.push({
          section,
          item_id: item.id,
          text: [value(item.position) ?? value(item.institution) ?? value(item.name), value(item.company)]
            .filter(Boolean)
            .join(" — "),
          reason: `Not a ${SECTION_NOUN[section]} on your resume.`,
          source: "rule",
          target: { kind: "item", section, itemId: item.id },
        });
        continue;
      }

      for (const field of [...STRICT_FIELDS[section], ...OMITTABLE_FIELDS[section]]) {
        const now = value(item[field]);
        const was = value(original[field]);
        if (now === was) continue;
        if (now === null && OMITTABLE_FIELDS[section].includes(field)) continue;
        findings.push({
          section,
          item_id: item.id,
          text: now ?? "",
          reason:
            now === null
              ? `Removed the ${FIELD_LABELS[field]} "${was}".`
              : was === null
                ? `Added a ${FIELD_LABELS[field]} ("${now}") that is not on your resume.`
                : `Changed the ${FIELD_LABELS[field]} from "${was}" to "${now}".`,
          source: "rule",
          target: { kind: "field", section, itemId: item.id, field },
        });
      }
    }
  }

  // Skills: a reordered subset of the base skills, nothing more.
  const known = new Set(base.skills.map((skill) => skillName(skill.name)));
  tailored.skills.forEach((skill, index) => {
    if (known.has(skillName(skill.name))) return;
    findings.push({
      section: "skills",
      item_id: null,
      text: skill.name,
      reason: `${skill.name.trim()} is not a skill on your resume.`,
      source: "rule",
      target: { kind: "skill", index },
    });
  });

  // Rewritten lines: every number, link and named thing must already be in
  // the base item (or, for the summary, the base resume).
  const scoped = scopes(base);
  const resumeLinks = normalizeLinkCorpus(scoped.resume.text);

  for (const claim of collectClaims(base, tailored)) {
    if (claim.verbatim) continue;
    const within = claim.itemId ? scoped.items.get(claim.itemId) : scoped.resume;
    if (!within) continue; // an unknown item is reported above

    const numbers = extractMetrics(claim.text)
      .filter((metric) => !metric.keys.some((key) => within.metrics.has(key)))
      .map((metric) => metric.raw.trim());
    const links = extractLinks(claim.text).filter((link) => !resumeLinks.includes(link));
    const terms = extractTerms(claim.text).filter((term) => !termIn(term, within.lower));

    const problems = [...new Set([...numbers, ...terms])];
    const where = claim.itemId
      ? `this ${SECTION_NOUN[claim.section as ItemSection]} on your resume`
      : "your resume";

    const reasons: string[] = [];
    if (problems.length) {
      reasons.push(
        `Mentions ${listPhrase(problems)}, which ${problems.length === 1 ? "isn't" : "aren't"} in ${where}.`,
      );
    }
    if (links.length) {
      reasons.push(`Links to ${listPhrase(links)}, which ${links.length === 1 ? "isn't" : "aren't"} on your resume.`);
    }
    if (reasons.length) {
      findings.push({
        section: claim.section,
        item_id: claim.itemId,
        text: claim.text,
        reason: reasons.join(" "),
        source: "rule",
        target: { kind: "text", key: claim.key },
      });
    }
  }

  return findings;
}

function normalizeLinkCorpus(text: string): string {
  return text.toLowerCase().replace(/https?:\/\//g, "").replace(/\bwww\./g, "");
}

function skillName(name: string): string {
  return name.normalize("NFKC").toLowerCase().replace(/\s+/g, " ").trim();
}

// --- The model check -------------------------------------------------------------

const JUDGE_INSTRUCTIONS = `You audit a tailored resume line by line against the candidate's original resume. The original is the only source of truth about the candidate.

For each claim, decide whether the ORIGINAL ITEM it belongs to supports it. For summary claims, the whole original resume is the evidence.

Supported: every fact in the claim is stated in, or directly and unambiguously implied by, the evidence. Rewording, shortening, reordering, merging facts from the same item, and an equivalent term ("Postgres" for "PostgreSQL", "built" for "implemented") are all fine.

Unsupported, if the claim has any of these:
- a tool, technology, language, framework, method, domain or skill the evidence does not mention for this item;
- a number, metric, percentage, team size, duration, scale or date the evidence does not state — including numbers computed from the evidence ("840 ms to 190 ms" is not "77% faster");
- an outcome, impact, customer or scope the evidence does not state;
- more ownership or seniority than the evidence shows ("contributed to" is not "led", "helped" is not "owned", "worked with" is not "managed");
- a fact that comes from a different item of the resume.

Be strict: when unsure, it is unsupported. The candidate will send this resume to employers under their own name.

reason: for an unsupported claim, one short sentence naming exactly what is not supported, e.g. "The original does not mention Kubernetes." For a supported claim, null.
Return one verdict for every claim id.`;

const Verdicts = z.object({
  verdicts: z.array(
    z.object({
      claim_id: z.string(),
      supported: z.boolean(),
      reason: z.string().nullable(),
    }),
  ),
});

export type Verdict = { supported: boolean; reason: string | null };

/**
 * Judges claims against the base resume. Injected, so the pipeline can be
 * tested without a model; the default makes one `verify` call per resume.
 */
export type Judge = (
  input: { base: Resume; claims: Claim[]; changes: TailorChange[] },
  context: LlmContext,
) => Promise<Map<string, Verdict>>;

function itemEvidence(base: Resume, section: TailorSection, itemId: string | null): string {
  if (!itemId) return "(the whole original resume, above)";
  const work = base.work.find((item) => item.id === itemId);
  if (work) {
    return [
      `${work.position} — ${work.company}${work.location ? ` (${work.location})` : ""}`,
      [work.startDate, work.endDate].filter(Boolean).join(" – "),
      work.summary,
      ...work.highlights.map((line) => `• ${line}`),
    ]
      .filter(Boolean)
      .join("\n");
  }
  const project = base.projects.find((item) => item.id === itemId);
  if (project) {
    return [
      `${project.name}${project.url ? ` — ${project.url}` : ""}`,
      project.description,
      ...project.highlights.map((line) => `• ${line}`),
    ]
      .filter(Boolean)
      .join("\n");
  }
  const education = base.education.find((item) => item.id === itemId);
  if (education) {
    return [
      [education.studyType, education.area, education.institution].filter(Boolean).join(", "),
      [education.startDate, education.endDate].filter(Boolean).join(" – "),
      education.score,
      ...education.highlights.map((line) => `• ${line}`),
    ]
      .filter(Boolean)
      .join("\n");
  }
  return `(no ${section} item with this id)`;
}

export const modelJudge: Judge = async ({ base, claims, changes }, context) => {
  const ids = new Map(claims.map((claim, index) => [`c${index + 1}`, claim]));

  // The tailor's own change log pairs a rewrite with the bullet it came from,
  // which makes the comparison far easier to get right.
  const origins = new Map<string, string>();
  for (const change of changes) {
    if (change.after && change.before) origins.set(normalizeText(change.after), change.before);
  }

  const groups = new Map<string, Array<[string, Claim]>>();
  for (const [id, claim] of ids) {
    const key = `${claim.section}:${claim.itemId ?? ""}`;
    groups.set(key, [...(groups.get(key) ?? []), [id, claim]]);
  }

  const blocks = [...groups.values()].map((group) => {
    const first = group[0][1];
    const claimsXml = group
      .map(([id, claim]) => {
        const origin = origins.get(normalizeText(claim.text));
        return `<claim id="${id}">\n${claim.text}${origin ? `\n<rewritten_from>${origin}</rewritten_from>` : ""}\n</claim>`;
      })
      .join("\n");
    return `<item section="${first.section}"${first.itemId ? ` id="${first.itemId}"` : ""}>
<original>
${itemEvidence(base, first.section, first.itemId)}
</original>
${claimsXml}
</item>`;
  });

  const { value: result } = await generateStructured(
    "verify",
    Verdicts,
    {
      instructions: `${JUDGE_INSTRUCTIONS}\n\n<original_resume>\n${resumeToText(base)}\n</original_resume>`,
      prompt: `Check these claims.\n\n${blocks.join("\n\n")}`,
    },
    context,
  );

  const verdicts = new Map<string, Verdict>();
  for (const verdict of result.verdicts) {
    const claim = ids.get(verdict.claim_id.trim());
    if (claim) {
      verdicts.set(claim.key, { supported: verdict.supported, reason: verdict.reason });
    }
  }
  return verdicts;
};

export type VerifyResult = {
  /** Every problem found, rules first, one per location. */
  findings: Finding[];
  checked: number;
  modelChecked: number;
  modelError: string | null;
};

/**
 * Both passes over a tailored resume. Rules first; the model only judges the
 * rewritten lines that passed them, so it is never asked about a line that is
 * going to be reverted anyway.
 */
export async function verifyTailored(
  base: Resume,
  tailored: Resume,
  changes: TailorChange[],
  context: LlmContext = {},
  deps: { judge?: Judge } = {},
): Promise<VerifyResult> {
  const findings = checkRules(base, tailored);
  const claims = collectClaims(base, tailored);
  const flagged = new Set(
    findings.flatMap((finding) => (finding.target.kind === "text" ? [finding.target.key] : [])),
  );
  const toJudge = claims.filter((claim) => !claim.verbatim && !flagged.has(claim.key));

  let modelError: string | null = null;

  if (toJudge.length > 0) {
    const judge = deps.judge ?? modelJudge;
    try {
      const verdicts = await judge({ base, claims: toJudge, changes }, context);
      for (const claim of toJudge) {
        const verdict = verdicts.get(claim.key);
        if (verdict?.supported) continue;
        findings.push(
          textFinding(
            claim,
            verdict
              ? verdict.reason?.trim() || "Not supported by your resume."
              : "The check returned no verdict for this line, so it was not trusted.",
            "model",
          ),
        );
      }
    } catch (error) {
      // An unchecked rewrite is not a verified one. Rather than save lines no
      // one has checked, every rewrite goes back to the original wording.
      modelError = error instanceof Error ? error.message : String(error);
      for (const claim of toJudge) {
        findings.push(
          textFinding(claim, "Could not be checked, so your original wording was kept.", "model"),
        );
      }
    }
  }

  return {
    findings,
    checked: claims.length + tailored.skills.length,
    modelChecked: toJudge.length,
    modelError,
  };
}

function textFinding(claim: Claim, reason: string, source: "rule" | "model"): Finding {
  return {
    section: claim.section,
    item_id: claim.itemId,
    text: claim.text,
    reason,
    source,
    target: { kind: "text", key: claim.key },
  };
}

// --- The revision -----------------------------------------------------------------

const STOPWORDS = new Set(
  "a an and the of for to in on at by with from into over under as or that which this than their its our your was were is are be been used using".split(" "),
);

/**
 * A crude suffix stemmer — enough that "partitioned" and "partitioning" count
 * as the same word when matching a rewrite back to the bullet it came from.
 */
function stem(word: string): string {
  for (const suffix of ["ments", "ment", "ions", "ion", "ing", "ies", "ed", "es", "ly", "s"]) {
    if (word.endsWith(suffix) && word.length - suffix.length >= 3) {
      return word.slice(0, -suffix.length);
    }
  }
  return word;
}

function contentWords(text: string): Set<string> {
  return new Set(
    normalizeText(text)
      .split(/[^\p{L}\p{N}.%]+/u)
      .map((word) => word.replace(/\.+$/, ""))
      .filter((word) => word.length > 2 && !STOPWORDS.has(word))
      .map(stem),
  );
}

/** Dice similarity over content words: which original a rewrite came from. */
export function similarity(a: string, b: string): number {
  const left = contentWords(a);
  const right = contentWords(b);
  if (left.size === 0 || right.size === 0) return 0;
  let shared = 0;
  for (const word of left) if (right.has(word)) shared += 1;
  return (2 * shared) / (left.size + right.size);
}

/** Below this, a flagged line resembles no original bullet and is dropped. */
const MIN_RESEMBLANCE = 0.15;

function publicPart(finding: Finding): UnsupportedClaim {
  return {
    section: finding.section,
    item_id: finding.item_id,
    text: finding.text,
    reason: finding.reason,
    source: finding.source,
  };
}

/**
 * The one automatic revision. Deterministic, and it only ever moves the
 * resume towards the base: a flagged line becomes the original bullet it most
 * resembles (or is dropped), a changed field goes back to its base value, an
 * unknown item or skill is removed. It never writes new text.
 */
export function reviseTailored(
  base: Resume,
  tailored: Resume,
  findings: Finding[],
): { resume: Resume; revisions: Revision[] } {
  const resume = structuredClone(tailored);
  const revisions: Revision[] = [];
  const record = (finding: Finding, action: Revision["action"], replacement: string | null) =>
    revisions.push({ ...publicPart(finding), action, replacement });

  // Basics.
  for (const finding of findings) {
    if (finding.target.kind !== "basics") continue;
    const field = finding.target.field;
    if (field === "links") {
      resume.basics.links = structuredClone(base.basics.links);
      record(finding, "reverted", base.basics.links.map((link) => link.url).join(", "));
    } else {
      resume.basics[field] = base.basics[field] as never;
      record(finding, "reverted", value(base.basics[field]));
    }
  }

  // Unknown items.
  const unknown = new Set(
    findings.flatMap((finding) =>
      finding.target.kind === "item" ? [`${finding.target.section}:${finding.target.itemId}`] : [],
    ),
  );
  if (unknown.size > 0) {
    resume.work = resume.work.filter((item) => !unknown.has(`work:${item.id}`));
    resume.education = resume.education.filter((item) => !unknown.has(`education:${item.id}`));
    resume.projects = resume.projects.filter((item) => !unknown.has(`projects:${item.id}`));
    for (const finding of findings) if (finding.target.kind === "item") record(finding, "dropped", null);
  }

  // Changed fields.
  for (const finding of findings) {
    const target = finding.target;
    if (target.kind !== "field") continue;
    const original = (base[target.section] as Array<Record<string, unknown>>).find(
      (item) => item.id === target.itemId,
    );
    const item = (resume[target.section] as Array<Record<string, unknown>>).find(
      (entry) => entry.id === target.itemId,
    );
    if (!original || !item) continue;
    item[target.field] = original[target.field] ?? null;
    record(finding, "reverted", value(original[target.field]));
  }

  // Unknown skills.
  const badSkills = new Set(
    findings.flatMap((finding) => (finding.target.kind === "skill" ? [finding.target.index] : [])),
  );
  if (badSkills.size > 0) {
    resume.skills = resume.skills.filter((_, index) => !badSkills.has(index));
    for (const finding of findings) if (finding.target.kind === "skill") record(finding, "dropped", null);
  }

  // Lines. Keys index the pre-revision resume, which is `tailored` — the
  // structures above only removed whole items, never lines within them.
  const byKey = new Map<string, Finding>();
  for (const finding of findings) {
    if (finding.target.kind === "text" && !byKey.has(finding.target.key)) {
      byKey.set(finding.target.key, finding);
    }
  }
  if (byKey.size === 0) return { resume, revisions };

  const flaggedAt = (key: string) => byKey.get(key);

  // The summary: drop unsupported sentences; if none survive, use the base
  // summary rather than none at all.
  if (resume.summary) {
    const sentences = splitSentences(resume.summary);
    const kept: string[] = [];
    sentences.forEach((sentence, index) => {
      const finding = flaggedAt(`summary:sentence:${index}`);
      if (finding) record(finding, "dropped", null);
      else kept.push(sentence);
    });
    if (kept.length < sentences.length) {
      if (kept.length > 0) {
        resume.summary = kept.join(" ");
      } else {
        resume.summary = base.summary ?? null;
        const last = revisions.at(-1);
        if (last && base.summary) {
          last.action = "replaced";
          last.replacement = base.summary;
        }
      }
    }
  }

  const reviseBullets = (
    section: ItemSection,
    itemId: string,
    highlights: string[],
    originals: string[],
  ): string[] => {
    const flagged = (index: number) => flaggedAt(`${section}:${itemId}:highlight:${index}`);
    // Originals already on the resume word for word are not candidates:
    // putting one back again would duplicate it.
    const used = new Set(
      highlights.filter((_, index) => !flagged(index)).map(normalizeText),
    );
    const next: string[] = [];
    highlights.forEach((line, index) => {
      const finding = flagged(index);
      if (!finding) {
        next.push(line);
        return;
      }
      let best: string | null = null;
      let bestScore = MIN_RESEMBLANCE;
      for (const original of originals) {
        if (used.has(normalizeText(original))) continue;
        const score = similarity(line, original);
        if (score >= bestScore) {
          best = original;
          bestScore = score;
        }
      }
      if (best) {
        used.add(normalizeText(best));
        next.push(best);
        record(finding, "replaced", best);
      } else {
        record(finding, "dropped", null);
      }
    });
    return next;
  };

  for (const item of resume.work) {
    const original = base.work.find((entry) => entry.id === item.id);
    if (!original) continue;
    const summaryFinding = flaggedAt(`work:${item.id}:summary:0`);
    if (summaryFinding) {
      item.summary = original.summary ?? null;
      record(summaryFinding, original.summary ? "replaced" : "dropped", original.summary ?? null);
    }
    item.highlights = reviseBullets("work", item.id, item.highlights, original.highlights);
  }

  for (const item of resume.projects) {
    const original = base.projects.find((entry) => entry.id === item.id);
    if (!original) continue;
    const descriptionFinding = flaggedAt(`projects:${item.id}:description:0`);
    if (descriptionFinding) {
      item.description = original.description ?? null;
      record(
        descriptionFinding,
        original.description ? "replaced" : "dropped",
        original.description ?? null,
      );
    }
    item.highlights = reviseBullets("projects", item.id, item.highlights, original.highlights);
  }

  for (const item of resume.education) {
    const original = base.education.find((entry) => entry.id === item.id);
    if (!original) continue;
    item.highlights = reviseBullets("education", item.id, item.highlights, original.highlights);
  }

  return { resume, revisions };
}

/**
 * The stored verification: the deterministic re-check of the revised resume,
 * plus the record of what was fixed to get there.
 */
export function finalizeVerification(input: {
  base: Resume;
  final: Resume;
  result: VerifyResult;
  revisions: Revision[];
  baseResumeId?: string | null;
  baseVersion?: number | null;
}): Verification {
  const remaining = checkRules(input.base, input.final);
  return {
    passed: remaining.length === 0,
    checkedAt: new Date().toISOString(),
    unsupported: remaining.map(publicPart),
    revised: input.revisions.length > 0,
    checked: input.result.checked,
    modelChecked: input.result.modelChecked,
    revisions: input.revisions,
    modelError: input.result.modelError,
    baseResumeId: input.baseResumeId ?? null,
    baseVersion: input.baseVersion ?? null,
  };
}

/**
 * The change log, made true to the saved resume.
 *
 * The tailor's own log describes its draft. Entries for lines the revision
 * took back are removed, entries about items that are not on the resume are
 * removed, evidence ids that are not base ids are dropped, and a rewrite the
 * tailor forgot to log gets an entry — the log is the audit trail, so it has
 * to cover every line that differs from the base.
 */
export function reconcileChanges(input: {
  base: Resume;
  final: Resume;
  changes: TailorChange[];
  revisions: Revision[];
}): TailorChange[] {
  const { base, final } = input;
  const known = new Set(factIds(base));
  const reverted = new Set(input.revisions.map((revision) => normalizeText(revision.text)));

  const finalTexts = new Set(
    collectClaims(base, final).map((claim) => normalizeText(claim.text)),
  );
  if (final.summary) finalTexts.add(normalizeText(final.summary));

  const out: TailorChange[] = [];
  for (const change of input.changes) {
    if (change.item_id && !known.has(change.item_id)) continue;
    const evidence = change.evidence_fact_ids.filter((id) => known.has(id));

    if (change.section === "summary") {
      // The summary is logged whole; after sentence-level revision it is
      // re-stated from the final text below.
      continue;
    }
    if (change.after) {
      const after = normalizeText(change.after);
      if (reverted.has(after)) continue;
      if (change.section !== "skills" && !finalTexts.has(after)) continue;
    }

    out.push({
      ...change,
      evidence_fact_ids: evidence.length ? evidence : change.item_id ? [change.item_id] : [],
    });
  }

  // The summary, as it finally stands.
  const summaryChanged =
    (final.summary ?? "") !== "" &&
    normalizeText(final.summary ?? "") !== normalizeText(base.summary ?? "");
  if (summaryChanged) {
    const logged = input.changes.find((change) => change.section === "summary");
    out.unshift({
      section: "summary",
      item_id: null,
      before: base.summary ?? null,
      after: final.summary ?? null,
      reason: logged?.reason || "Rewritten for this posting from facts on your resume.",
      evidence_fact_ids: (logged?.evidence_fact_ids ?? []).filter((id) => known.has(id)),
    });
  }

  // Rewrites the tailor did not log.
  const logged = new Set(out.flatMap((change) => (change.after ? [normalizeText(change.after)] : [])));
  for (const claim of collectClaims(base, final)) {
    if (claim.verbatim || claim.field === "sentence" || logged.has(normalizeText(claim.text))) continue;
    out.push({
      section: claim.section,
      item_id: claim.itemId,
      before: closestOriginal(base, claim),
      after: claim.text,
      reason: "Reworded for this posting.",
      evidence_fact_ids: claim.itemId ? [claim.itemId] : [],
    });
  }

  return out;
}

function closestOriginal(base: Resume, claim: Claim): string | null {
  const item = [...base.work, ...base.projects, ...base.education].find(
    (entry) => entry.id === claim.itemId,
  ) as ({ highlights: string[]; summary?: string | null; description?: string | null } | undefined);
  if (!item) return null;
  if (claim.field === "summary") return item.summary ?? null;
  if (claim.field === "description") return item.description ?? null;
  let best: string | null = null;
  let bestScore = 0;
  for (const original of item.highlights) {
    const score = similarity(claim.text, original);
    if (score > bestScore) {
      best = original;
      bestScore = score;
    }
  }
  return best;
}
