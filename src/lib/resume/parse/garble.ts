/**
 * Is this extracted text usable, or do we need to look at the page?
 *
 * PDF text extraction fails in specific, measurable ways: a scanned page yields
 * almost nothing, a broken font map yields replacement characters, some
 * exporters glue every word together, and others letter-space everything into
 * single characters. Each of those is cheap to detect, and detecting them is
 * what decides whether to spend a vision call.
 *
 * What this deliberately does NOT claim to catch is *wrong reading order* — a
 * two-column layout extracted as interleaved columns is perfectly good text in
 * a scrambled sequence. No character-level statistic sees that, and a vision
 * pass is not obviously better at it than the language model reading the
 * interleaved text. That case is handled by the extraction prompt instead.
 */

export type GarbleReport = {
  garbled: boolean;
  /** Which signals tripped, for the log and for the fixtures' sake. */
  reasons: string[];
  metrics: {
    chars: number;
    charsPerPage: number;
    spaceRatio: number;
    alphaRatio: number;
    replacementRatio: number;
    singleCharWordRatio: number;
  };
};

/** Below this, a page is effectively blank — almost always a scan. */
const MIN_CHARS_PER_PAGE = 180;
/** English prose runs ~0.15. Much lower means words are running together. */
const MIN_SPACE_RATIO = 0.08;
/** Letters as a share of non-space characters. */
const MIN_ALPHA_RATIO = 0.5;
/** Replacement and control characters mean a broken font/encoding map. */
const MAX_REPLACEMENT_RATIO = 0.01;
/** Letter-spaced extraction turns every glyph into its own "word". */
const MAX_SINGLE_CHAR_WORD_RATIO = 0.4;

/** U+FFFD, plus control characters that should never survive extraction. */
const BROKEN_ENCODING = new RegExp(
  "[\\uFFFD\\u0000-\\u0008\\u000B\\u000C\\u000E-\\u001F]",
  "g",
);

export function detectGarble(text: string, pageCount: number): GarbleReport {
  const chars = text.length;
  const pages = Math.max(pageCount, 1);

  const spaces = (text.match(/\s/g) ?? []).length;
  const alpha = (text.match(/\p{L}/gu) ?? []).length;
  const replacement = (text.match(BROKEN_ENCODING) ?? []).length;

  const words = text.split(/\s+/).filter(Boolean);
  const singleCharWords = words.filter((word) => word.length === 1).length;

  const nonSpace = Math.max(chars - spaces, 1);

  const metrics = {
    chars,
    charsPerPage: chars / pages,
    spaceRatio: chars === 0 ? 0 : spaces / chars,
    alphaRatio: alpha / nonSpace,
    replacementRatio: chars === 0 ? 0 : replacement / chars,
    singleCharWordRatio: words.length === 0 ? 0 : singleCharWords / words.length,
  };

  const reasons: string[] = [];
  if (metrics.charsPerPage < MIN_CHARS_PER_PAGE) {
    reasons.push(
      `only ${Math.round(metrics.charsPerPage)} characters per page — likely a scan or an image-only PDF`,
    );
  }
  if (chars > 0 && metrics.spaceRatio < MIN_SPACE_RATIO) {
    reasons.push(
      `word spacing lost (${metrics.spaceRatio.toFixed(3)} of characters are whitespace)`,
    );
  }
  if (chars > 0 && metrics.alphaRatio < MIN_ALPHA_RATIO) {
    reasons.push(
      `mostly non-letters (${metrics.alphaRatio.toFixed(2)} alphabetic)`,
    );
  }
  if (metrics.replacementRatio > MAX_REPLACEMENT_RATIO) {
    reasons.push("replacement or control characters — broken font encoding");
  }
  if (metrics.singleCharWordRatio > MAX_SINGLE_CHAR_WORD_RATIO) {
    reasons.push(
      `text is letter-spaced (${(metrics.singleCharWordRatio * 100).toFixed(0)}% single-character words)`,
    );
  }

  return { garbled: reasons.length > 0, reasons, metrics };
}
