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

/** The deep evaluation's verdict. Agrees with the score band by construction. */
export const Recommendation = z.enum(["strong", "apply", "maybe", "skip"]);
export type Recommendation = z.infer<typeof Recommendation>;

export const MatchedRequirement = z.object({
  /** What the posting asks for, in its own terms. */
  requirement: z.string(),
  /** The resume evidence that meets it, quoted or closely paraphrased. */
  evidence: z.string(),
  /** Bracketed ids of the resume items the evidence comes from. */
  fact_ids: z.array(z.string()),
  strength: z.enum(["strong", "partial"]),
});

export type MatchedRequirement = z.infer<typeof MatchedRequirement>;

export const EvalGap = z.object({
  gap: z.string(),
  /**
   * `disqualifying` only for a hard requirement the candidate clearly fails or
   * one of their own dealbreakers; everything else is `learnable`.
   */
  severity: z.enum(["learnable", "disqualifying"]),
  note: z.string(),
});

export type EvalGap = z.infer<typeof EvalGap>;

/**
 * The strong-tier pass, over the top few matches rather than all of them.
 *
 * Evidence comes before the verdict in the schema on purpose: models write the
 * fields in order, and a score written after the requirements have been laid
 * out is anchored to them rather than to a first impression.
 *
 * The three `*_fit` fields are relations, so their direction is fixed here:
 * - seniority_fit is the candidate against the posting's level — `under` means
 *   the role asks for more experience than they have, `over` that they are
 *   overqualified for it.
 * - salary_fit is the posting's pay against the candidate's floor — `above`
 *   means it pays more than their floor.
 */
export const DeepEval = z.object({
  matched_requirements: z.array(MatchedRequirement),
  gaps: z.array(EvalGap),
  seniority_fit: z
    .enum(["under", "match", "over", "unclear"])
    .describe(
      "The candidate against the posting's level: under = the role asks for more experience than they have; over = they are overqualified.",
    ),
  location_fit: z.enum(["match", "partial", "mismatch", "unclear"]),
  salary_fit: z
    .enum(["above", "within", "below", "unstated"])
    .describe(
      "The posting's pay against the candidate's salary floor: above = pays more than their floor; below = pays less.",
    ),
  risk_flags: z.array(z.string()),
  /** What to emphasise when applying. Read later by resume tailoring. */
  talking_points: z.array(z.string()),
  /** Two or three plain sentences, specific to this person and this role. */
  summary: z.string(),
  /** 0–100, on the rubric's calibrated scale. */
  score: z.number().int().min(0).max(100),
  recommendation: Recommendation,
});

export type DeepEval = z.infer<typeof DeepEval>;
