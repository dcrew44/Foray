import { and, eq } from "drizzle-orm";

import { db } from "@/lib/db";
import { jobMatches, jobs } from "@/lib/db/schema";
import { saveDeepEval } from "@/lib/db/queries/matches";
import { getProfile, loadBaseResume } from "@/lib/db/queries/profile";
import type { CallOverrides } from "@/lib/llm/call";
import type { LlmContext } from "@/lib/llm/context";
import { modelRef } from "@/lib/llm/models";
import { costUsd, rateFor } from "@/lib/llm/pricing";
import { generateStructured } from "@/lib/llm/structured";
import {
  EMPTY_PREFERENCES,
  PreferencesSchema,
  describePreferences,
  type Preferences,
} from "@/lib/profile/interview";
import { factIds } from "@/lib/resume/schema";
import { resumeToText } from "@/lib/resume/text";

import { DeepEval } from "./schemas";

/**
 * The strong-tier pass: one posting, read in full, against the whole resume.
 *
 * Where the fast score triages hundreds of postings on a truncated description,
 * this reads the top few properly and has to show its working — every matched
 * requirement names the resume items it rests on, by id, so a claim that a
 * candidate "has led migrations" can be traced to the bullet that says so. The
 * same ids let tailoring later reuse the evidence rather than rediscover it.
 *
 * It costs a few cents a posting on a strong model, which is why a search asks
 * before running it (see runs/approvals.ts) and why the approval quotes a
 * price from the prompt this file actually builds.
 */

/** How much of a posting the deep pass reads — effectively all of it. */
export const MAX_DESCRIPTION_CHARS = 12_000;

/**
 * A ceiling on one call. Evaluations take tens of seconds; one that hangs
 * must fail on its own rather than hold the whole batch open.
 */
const CALL_TIMEOUT_MS = 4 * 60 * 1000;

const RUBRIC = `You evaluate one job posting for one candidate, in depth. Your verdict decides whether they spend an application on it, so be specific, honest and decisive.

Score 0–100 on this scale, which is not a feeling:
- 85–100 (strong): squarely what they asked for, and the resume shows direct
  evidence for the posting's core requirements.
- 70–84 (apply): a real fit. Most core requirements are met with evidence; the
  gaps are learnable or minor.
- 50–69 (maybe): plausible but uncertain — an adjacent domain, a level that is
  somewhat off, or a posting too vague to judge.
- 0–49 (skip): not worth an application — a disqualifying gap, the wrong level
  or discipline, or one of their dealbreakers.
The recommendation is the band's label and always agrees with the score.

Requirements and evidence:
- matched_requirements: requirements the posting actually states (required or
  preferred), each with the resume evidence that meets it. Quote or closely
  paraphrase the resume; never credit experience the resume does not show.
  fact_ids are the bracketed ids of the resume items the evidence comes from,
  copied exactly — only ids that appear in the resume. strength is "strong"
  when the evidence meets the requirement outright, "partial" when it is
  adjacent or smaller in scale.
- gaps: stated requirements the resume does not show. "disqualifying" only for
  a hard requirement they clearly fail (a degree, a clearance, work
  authorisation, years of experience far beyond theirs) or a conflict with one
  of their dealbreakers; everything else is "learnable". note says why, in one
  sentence.
- If the posting does not state something, it is unknown, not a miss. A
  technology absent from the resume is a gap only if the posting requires it.
- Any disqualifying gap means skip — or maybe, if you are unsure it is truly
  required.

Fit:
- seniority_fit: the candidate against the posting's level. "under" when it
  asks for more experience than they have, "over" when they are overqualified,
  "unclear" when the posting does not say.
- location_fit: the posting's location and remote policy against theirs.
  "partial" for, say, hybrid when they want remote, or a nearby metro.
  "unclear" when the posting does not say.
- salary_fit: the posting's pay against their salary floor — "above",
  "within" (the floor falls inside the range) or "below". "unstated" when the
  posting gives no pay or they gave no floor.

Writing — the candidate reads this themselves, on the job's page:
- Write summary, gap notes, risk_flags and talking_points in the second
  person, speaking to the candidate: "You have just over five years of
  backend work…", "Lead with your ingestion-service rebuild…". Never refer to
  them by name or as "the candidate", "they" or "he/she". evidence is the
  exception: keep it a close quote or paraphrase of the resume.
- summary: two or three plain sentences on why this role fits them or does
  not. Specific; no generic praise, no restating the title.
- risk_flags: concrete things worth knowing before applying (a relocation
  requirement, contract-to-hire, heavy on-call, a posting open for months).
  Empty when there are none.
- talking_points: three to five things to emphasise when applying, each tied
  to a specific resume item and to what this posting asks for.
- Resume ids belong in fact_ids and nowhere else. Every other field is read by
  a person: never write an id, or a bracketed reference to one, in summary,
  requirement, evidence, gap, note, risk_flags or talking_points.`;

// --- The prompt ----------------------------------------------------------------

/** Who is being evaluated. Loaded once per batch: it is the same for every posting. */
export type EvalCandidate = {
  resumeId: string;
  resumeVersion: number;
  /** `resumeToText` output — items carry their `[id]`. */
  resumeText: string;
  preferences: Preferences;
  /** Every fact id in the resume. Citations outside this set are dropped. */
  factIds: ReadonlySet<string>;
};

export type EvalPosting = {
  title: string;
  company: string;
  locations: string[];
  remote: string;
  employmentType: string | null;
  salaryMin: string | number | null;
  salaryMax: string | number | null;
  salaryCurrency: string | null;
  salaryPeriod: string | null;
  postedAt: Date | null;
  description: string | null;
};

/**
 * The half of the prompt that is identical for every posting in a batch —
 * rubric and candidate first, so a provider's prompt cache can reuse it.
 */
export function evalInstructions(candidate: EvalCandidate): string {
  return `${RUBRIC}

<candidate>
What they are looking for:
${describePreferences(candidate.preferences)}

Resume (each item's [id] is what fact_ids cite):
${candidate.resumeText}
</candidate>`;
}

/** The per-posting half. */
export function evalPrompt(posting: EvalPosting, now: Date = new Date()): string {
  const description = posting.description?.trim() ?? "";
  const truncated = description.length > MAX_DESCRIPTION_CHARS;

  return `<posting>
Title: ${posting.title}
Company: ${posting.company}
Location: ${posting.locations.join(" | ") || "unstated"} (${posting.remote})
Employment type: ${posting.employmentType ?? "unstated"}
Pay: ${formatPay(posting)}
Posted: ${formatPosted(posting.postedAt, now)}
Today: ${now.toISOString().slice(0, 10)}

${description ? description.slice(0, MAX_DESCRIPTION_CHARS) : "(no description)"}${
    truncated ? `\n[Description cut at ${MAX_DESCRIPTION_CHARS.toLocaleString("en-US")} characters.]` : ""
  }
</posting>`;
}

function formatPay(posting: EvalPosting): string {
  const min = toNumber(posting.salaryMin);
  const max = toNumber(posting.salaryMax);
  if (min == null && max == null) return "unstated";

  const fmt = (n: number) => n.toLocaleString("en-US");
  const range =
    min != null && max != null && min !== max
      ? `${fmt(min)}–${fmt(max)}`
      : fmt((min ?? max)!);
  return `${range} ${posting.salaryCurrency ?? ""} per ${posting.salaryPeriod ?? "unstated period"}`.replace(
    /\s+/g,
    " ",
  );
}

function formatPosted(postedAt: Date | null, now: Date): string {
  if (!postedAt) return "unknown";
  const days = Math.max(
    0,
    Math.floor((now.getTime() - postedAt.getTime()) / 86_400_000),
  );
  return `${postedAt.toISOString().slice(0, 10)} (${days} ${days === 1 ? "day" : "days"} ago)`;
}

function toNumber(value: string | number | null): number | null {
  if (value == null) return null;
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : null;
}

// --- Running it ----------------------------------------------------------------

/**
 * Makes an evaluation safe to store and show.
 *
 * Citations: the model is told to copy ids exactly, and mostly does; the ones
 * it invents or mangles would otherwise surface as citations that resolve to
 * nothing, and tailoring reads these ids later. Brackets copied along with an
 * id are forgiven rather than treated as invention.
 *
 * Prose: the resume it reads carries `[id]` on every item, and models echo
 * those into sentences ("…cut latency by 70% [3d98…]"). Every prose field is
 * read by a person, so bracketed ids are stripped from all of them — after
 * any valid id found in a requirement's evidence has been moved into its
 * `fact_ids`, so the citation survives the clean-up.
 */
export function sanitizeDeepEval(
  value: DeepEval,
  validIds: ReadonlySet<string>,
): DeepEval {
  const prose = (text: string) => stripFactIds(text, validIds);
  const cleanList = (items: string[]) => items.map(prose).filter(Boolean);

  return {
    ...value,
    matched_requirements: value.matched_requirements.map((item) => ({
      ...item,
      requirement: prose(item.requirement),
      evidence: prose(item.evidence),
      fact_ids: [
        ...new Set(
          [...item.fact_ids, ...idsIn(item.evidence)]
            .map((id) => id.trim().replace(/^\[|\]$/g, "").trim())
            .filter((id) => validIds.has(id)),
        ),
      ],
    })),
    gaps: value.gaps.map((gap) => ({
      ...gap,
      gap: prose(gap.gap),
      note: prose(gap.note),
    })),
    risk_flags: cleanList(value.risk_flags),
    talking_points: cleanList(value.talking_points),
    summary: prose(value.summary),
  };
}

/** One or more UUIDs in brackets, e.g. `[3d98…]` or `[3d98…, 71ac…]`. */
const BRACKETED_UUIDS =
  /\s*\[\s*[0-9a-f]{8}-[0-9a-f-]{27,}(?:\s*[,;]\s*[0-9a-f]{8}-[0-9a-f-]{27,})*\s*\]/gi;
const UUID_IN_TEXT = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi;

/** The UUIDs a model wrote inside brackets in a piece of prose. */
function idsIn(text: string): string[] {
  return (text.match(BRACKETED_UUIDS) ?? []).flatMap(
    (group) => group.match(UUID_IN_TEXT) ?? [],
  );
}

/**
 * Removes bracketed ids from prose: any bracketed UUID, and any bracketed id
 * the resume actually uses (resume ids are UUIDs in practice, but nothing
 * guarantees it).
 */
export function stripFactIds(
  text: string,
  validIds: ReadonlySet<string> = new Set(),
): string {
  let out = text.replace(BRACKETED_UUIDS, "");
  for (const id of validIds) {
    if (!out.includes(`[${id}]`)) continue;
    out = out.replace(
      new RegExp(`\\s*\\[${id.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\]`, "g"),
      "",
    );
  }
  return out.replace(/ {2,}/g, " ").trim();
}

/** One evaluation: the model call and the citation check, no database. */
export async function runDeepEval(input: {
  candidate: EvalCandidate;
  posting: EvalPosting;
  context?: LlmContext;
  overrides?: CallOverrides;
  now?: Date;
}): Promise<DeepEval> {
  const { value } = await generateStructured(
    "evaluate",
    DeepEval,
    {
      instructions: evalInstructions(input.candidate),
      prompt: evalPrompt(input.posting, input.now),
      timeout: CALL_TIMEOUT_MS,
    },
    input.context ?? {},
    input.overrides ?? {},
  );

  return sanitizeDeepEval(value, input.candidate.factIds);
}

export class NoResumeError extends Error {
  constructor() {
    super(
      "There is no resume on file to evaluate against. Upload one on the profile page first.",
    );
    this.name = "NoResumeError";
  }
}

/**
 * The candidate as the evaluation sees them: the current base resume and
 * their stated preferences. Null when there is no usable resume — an
 * evaluation without one could only guess.
 */
export async function loadEvalCandidate(
  userId: string,
): Promise<EvalCandidate | null> {
  const [base, profile] = await Promise.all([
    loadBaseResume(userId),
    getProfile(userId),
  ]);
  if (!base) return null;

  return {
    resumeId: base.id,
    resumeVersion: base.version,
    resumeText: resumeToText(base.resume),
    preferences:
      PreferencesSchema.safeParse(profile?.preferences).data ??
      EMPTY_PREFERENCES,
    factIds: new Set(factIds(base.resume)),
  };
}

export type MatchEvaluation = {
  matchId: string;
  jobId: string;
  title: string;
  company: string;
  evaluation: DeepEval;
};

/**
 * Evaluates one of the user's matches and stores the result on it
 * (`deep_score`, `deep_eval`). Throws when the match is not theirs, when there
 * is no resume, or when the model fails; the caller decides what a failure
 * means for its run.
 */
export async function evaluateMatch(input: {
  userId: string;
  matchId: string;
  context?: LlmContext;
  /** Pass when evaluating several, so the resume is loaded once. */
  candidate?: EvalCandidate;
  overrides?: CallOverrides;
}): Promise<MatchEvaluation> {
  const { userId, matchId } = input;

  const candidate = input.candidate ?? (await loadEvalCandidate(userId));
  if (!candidate) throw new NoResumeError();

  const [row] = await db
    .select({
      jobId: jobs.id,
      title: jobs.title,
      company: jobs.companyName,
      locations: jobs.locations,
      remote: jobs.remote,
      employmentType: jobs.employmentType,
      salaryMin: jobs.salaryMin,
      salaryMax: jobs.salaryMax,
      salaryCurrency: jobs.salaryCurrency,
      salaryPeriod: jobs.salaryPeriod,
      postedAt: jobs.postedAt,
      description: jobs.descriptionText,
    })
    .from(jobMatches)
    .innerJoin(jobs, eq(jobs.id, jobMatches.jobId))
    .where(and(eq(jobMatches.userId, userId), eq(jobMatches.id, matchId)))
    .limit(1);

  if (!row) throw new Error("That match does not exist.");

  const evaluation = await runDeepEval({
    candidate,
    posting: row,
    context: input.context ?? { userId },
    overrides: input.overrides,
  });

  const saved = await saveDeepEval(userId, matchId, evaluation);
  if (!saved) throw new Error("That match was removed during evaluation.");

  return {
    matchId,
    jobId: row.jobId,
    title: row.title,
    company: row.company,
    evaluation,
  };
}

// --- What it costs ---------------------------------------------------------------

export type EvalCostEstimate = {
  /** For all `n` evaluations. Null when the model has no price on file. */
  usd: number | null;
  perItemUsd: number | null;
  /** `provider:model`, or "unconfigured" when no evaluate model is set. */
  model: string;
};

/**
 * Output tokens one evaluation is assumed to spend, reasoning included.
 *
 * Measured 2026-09-24 over eight evaluations on gpt-5.6-terra at medium
 * effort: 1,370–2,126 output tokens (about 400 of them reasoning), $0.019–0.030
 * each. Output is most of the bill, so this sits above the average — a quote
 * that comes in under is fine, one that comes in over is not. Re-check
 * against `llm_calls` rows with purpose `evaluate` when the model changes.
 */
export const EVAL_OUTPUT_TOKENS = 1_800;
/** The JSON schema and message framing a structured call adds to the prompt. */
const SCHEMA_OVERHEAD_TOKENS = 400;
/** The usual rough conversion for English prose. */
const CHARS_PER_TOKEN = 4;
/** Header lines and tags around a posting's description. */
const POSTING_FRAME_CHARS = 400;
/**
 * A prompt of typical size, for when the real one is not to hand: the rubric,
 * a one-page resume with preferences, and a mid-length posting.
 */
export const TYPICAL_EVAL_INPUT_CHARS = RUBRIC.length + 6_500 + 7_000;

/**
 * The cost of `n` evaluations from the list price of the configured model.
 *
 * `inputChars` is the per-evaluation prompt size; pass the real one (see
 * `estimateEvalCostFor`) whenever the prompts are known. Provider prompt
 * caching is ignored, so the estimate errs high on input — which is the small
 * half of the bill anyway.
 */
export function estimateEvalCost(
  n: number,
  options: { inputChars?: number; modelRef?: string; at?: Date } = {},
): EvalCostEstimate {
  const ref = options.modelRef ?? evaluateModelRef();
  if (!ref) return { usd: null, perItemUsd: null, model: "unconfigured" };
  if (!rateFor(ref, options.at)) {
    return { usd: null, perItemUsd: null, model: ref };
  }

  const count = Math.max(0, Math.floor(n));
  const inputTokens =
    Math.ceil((options.inputChars ?? TYPICAL_EVAL_INPUT_CHARS) / CHARS_PER_TOKEN) +
    SCHEMA_OVERHEAD_TOKENS;

  const perItem = costUsd(
    ref,
    { inputTokens, outputTokens: EVAL_OUTPUT_TOKENS },
    options.at,
  );

  return {
    usd: roundUsd(perItem * count),
    perItemUsd: roundUsd(perItem),
    model: ref,
  };
}

/**
 * The estimate for specific postings, from the prompt that would actually be
 * sent: this candidate's instructions plus each description as the pass would
 * cut it.
 */
export function estimateEvalCostFor(
  candidate: EvalCandidate,
  descriptionChars: number[],
  options: { modelRef?: string; at?: Date } = {},
): EvalCostEstimate {
  const n = descriptionChars.length;
  if (n === 0) return estimateEvalCost(0, options);

  const instructions = evalInstructions(candidate).length;
  const total = descriptionChars.reduce(
    (sum, chars) =>
      sum + instructions + POSTING_FRAME_CHARS + Math.min(chars, MAX_DESCRIPTION_CHARS),
    0,
  );

  return estimateEvalCost(n, { ...options, inputChars: total / n });
}

/** "about $0.42", "under a cent", or null when there is no price. */
export function costPhrase(usd: number | null): string | null {
  if (usd == null) return null;
  if (usd < 0.01) return "under a cent";
  return `about $${usd.toFixed(2)}`;
}

function evaluateModelRef(): string | null {
  try {
    return modelRef("evaluate");
  } catch {
    return null;
  }
}

function roundUsd(value: number): number {
  return Math.round(value * 10_000) / 10_000;
}
