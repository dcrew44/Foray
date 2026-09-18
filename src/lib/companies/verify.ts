import { BOARD_ADAPTERS, type BoardSourceId } from "@/lib/sources";
import { BoardNotFoundError } from "@/lib/sources/types";

/**
 * Does this slug actually have a board, and does it have jobs on it?
 *
 * A company is only ever registered after a live call returns postings — that
 * is what keeps discovery's guesses from filling the registry with plausible
 * nonsense. The three outcomes are deliberately distinct: a 404 means the slug
 * is wrong, an empty board means the slug is right but nothing is open, and an
 * error means we do not know yet and should not conclude anything.
 */

export type VerifyOutcome =
  | { status: "ok"; source: BoardSourceId; slug: string; jobCount: number }
  | { status: "empty"; source: BoardSourceId; slug: string }
  | { status: "not-found"; source: BoardSourceId; slug: string }
  | { status: "error"; source: BoardSourceId; slug: string; error: string };

export async function verifyBoard(
  source: BoardSourceId,
  slug: string,
): Promise<VerifyOutcome> {
  try {
    const result = await BOARD_ADAPTERS[source].fetchBoard!(slug);
    return result.jobs.length > 0
      ? { status: "ok", source, slug, jobCount: result.jobs.length }
      : { status: "empty", source, slug };
  } catch (error) {
    if (error instanceof BoardNotFoundError) {
      return { status: "not-found", source, slug };
    }
    return {
      status: "error",
      source,
      slug,
      error: error instanceof Error ? error.message : String(error),
    };
  }
}

/**
 * Tries one slug against every board provider.
 *
 * Discovery guesses a slug from a company name and has no idea which ATS the
 * company uses, so all three are tried and the first that answers wins. A board
 * with jobs beats an empty one — an empty board is real but useless right now.
 */
export async function findBoard(
  slug: string,
  sources: BoardSourceId[] = ["greenhouse", "lever", "ashby"],
): Promise<VerifyOutcome | null> {
  const outcomes = await Promise.all(
    sources.map((source) => verifyBoard(source, slug)),
  );

  return (
    outcomes.find((outcome) => outcome.status === "ok") ??
    outcomes.find((outcome) => outcome.status === "empty") ??
    null
  );
}

/**
 * Plausible ATS slugs for a company name.
 *
 * Boards are overwhelmingly the company name lowercased with punctuation
 * removed, so a handful of variants covers most of them. Order matters: the
 * first that verifies is taken.
 */
export function slugCandidates(name: string): string[] {
  const base = name
    .trim()
    .toLowerCase()
    .replace(/[''’]/g, "")
    .replace(/&/g, "and")
    .replace(/\b(?:inc|llc|ltd|corp|corporation|co|gmbh|plc)\.?\b/g, " ")
    .trim();

  const words = base.split(/[^a-z0-9]+/).filter(Boolean);
  if (words.length === 0) return [];

  const joined = words.join("");
  const hyphenated = words.join("-");

  return [
    ...new Set([
      joined,
      hyphenated,
      words[0],
      `${joined}inc`,
      `${joined}hq`,
      `${joined}jobs`,
      `${joined}careers`,
    ]),
  ].filter((slug) => slug.length >= 2);
}
