import { z } from "zod";

/**
 * What the scoring passes return.
 *
 * Every field is required with absence expressed as null or an empty array:
 * OpenAI's strict structured output rejects optional properties, and a cheap
 * model given an optional field will simply omit it.
 */

export const Seniority = z.enum([
  "intern",
  "new_grad",
  "entry",
  "mid",
  "senior",
  "staff_plus",
  "unclear",
]);

/**
 * The cheap pass. Kept deliberately small — it runs over hundreds of postings,
 * so every extra field is paid for hundreds of times, and anything that needs
 * real reasoning belongs in the deep evaluation instead.
 */
export const FastScore = z.object({
  /** 0–100. Calibrated by the rubric in the instructions, not by vibes. */
  score: z.number().int().min(0).max(100),
  /** At most three, short. Why this could be a fit. */
  fit_reasons: z.array(z.string()),
  /** Concerns worth surfacing — not disqualifying on their own. */
  red_flags: z.array(z.string()),
  /** The posting's level, as the posting describes it. */
  seniority: Seniority,
  /**
   * Stated requirements the candidate demonstrably does not meet — a degree
   * they lack, a clearance, years of experience well beyond theirs. Only things
   * the posting states as requirements.
   */
  hard_requirement_misses: z.array(z.string()),
});

export type FastScore = z.infer<typeof FastScore>;
