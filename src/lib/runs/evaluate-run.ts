import { and, eq, inArray, sql } from "drizzle-orm";

import { db } from "@/lib/db";
import { runs, type Run } from "@/lib/db/schema";
import {
  getMatchRows,
  isShortlistCandidate,
  matchesNeedingEvaluation,
  moveMatchesFromStage,
  scoreBandsForJobs,
  topMatchIdsForJobs,
  type MatchRow,
} from "@/lib/db/queries/matches";
import { loadBaseResume } from "@/lib/db/queries/profile";
import { pooled } from "@/lib/pool";
import {
  QUEUES,
  type MatchEvaluatePayload,
  type SearchContinuationPayload,
} from "@/lib/queue/queues";
import { send } from "@/lib/queue/send";
import {
  NoResumeError,
  evaluateMatch,
  loadEvalCandidate,
  type EvalCandidate,
  type MatchEvaluation,
} from "@/lib/ranking/deep-eval";
import { parseBrief } from "@/lib/ranking/brief";
import type { Recommendation } from "@/lib/ranking/schemas";

import {
  approvedItemIds,
  getApproval,
  isApproved,
  requestApproval,
  type ShortlistPayload,
} from "./approvals";
import { emit } from "./events";
import {
  createRun,
  finishActiveRun,
  getRun,
  isCancelled,
  mergeStats,
  setRunStatusIf,
} from "./runs";

/**
 * Everything after a search run's first gate, and evaluation on demand.
 *
 * A search pauses twice in `approve_each`: before paying for deep evaluation,
 * and before moving anything onto the shortlist. Each pause ends the handler;
 * the decision enqueues one of the continuations here, which loads the run and
 * the approval back from the database and carries on. Nothing is held in
 * memory across a gate, so a worker restart between the two changes nothing.
 *
 * Every path ends the run — completed, failed, or quietly left alone because
 * the user cancelled it. A run is never left `running` by an exception.
 */

/**
 * Concurrent evaluations. Each takes tens of seconds on a strong model, so
 * this is what decides how long a batch feels; the per-provider limiter is the
 * real ceiling above it.
 */
const EVAL_CONCURRENCY = 5;
/** A progress event every this many evaluations. */
const PROGRESS_EVERY = 3;

/** What a completed search run's `output` holds. */
export type SearchRunOutput = {
  candidates: number;
  newMatches: number;
  scored: number;
  autoArchived: number;
  evaluated: number;
  shortlisted: number;
  /** Up to five of this run's best open matches, deep score preferred. */
  topMatchIds: string[];
};

/** What a completed `rank_deep` run's `output` holds. */
export type MatchEvaluateOutput = {
  evaluated: number;
  /** The matches that now carry a fresh evaluation. */
  matchIds: string[];
};

const RESUMABLE: Run["status"][] = ["queued", "running", "awaiting_approval"];

// --- Finishing a search run ------------------------------------------------------

/**
 * Completes a search run from whatever it recorded along the way.
 *
 * Counts are read back from `stats` rather than passed in, because by the
 * time a run completes the handler that scored it is long gone. Returns false
 * when the run was cancelled first, in which case nothing is reported.
 */
export async function completeSearchRun(
  ref: Pick<Run, "id" | "userId">,
): Promise<boolean> {
  const run = await getRun(ref.userId, ref.id);
  if (!run) return false;

  const stats = asRecord(run.stats);
  const count = (key: string) => {
    const value = stats[key];
    return typeof value === "number" && Number.isFinite(value) ? value : 0;
  };

  const candidateJobIds = candidateJobIdsOf(run);

  const output: SearchRunOutput = {
    candidates: count("candidates"),
    newMatches: count("newMatches"),
    scored: count("scored"),
    autoArchived: count("autoArchived"),
    evaluated: count("evaluated"),
    shortlisted: count("shortlisted"),
    topMatchIds: await topMatchIdsForJobs(run.userId, candidateJobIds, 5),
  };

  const bands =
    output.evaluated > 0
      ? { strong: 0, good: 0 }
      : await scoreBandsForJobs(run.userId, candidateJobIds);

  const finished = await finishActiveRun(
    run.id,
    { status: "completed", output },
    { phase: "done" },
  );
  if (!finished) return false;

  await emit({
    userId: run.userId,
    runId: run.id,
    kind: "run.completed",
    message: summarizeSearchRun({
      ...output,
      recommended: count("recommended"),
      ...bands,
    }),
    data: output,
  });
  return true;
}

/**
 * The run's closing line in the activity feed, e.g. "Found 60 roles, scored
 * 60, deep-evaluated 12. 5 look worth applying to; 4 shortlisted."
 */
export function summarizeSearchRun(
  o: SearchRunOutput & {
    /** Evaluated matches recommended strong or apply. */
    recommended: number;
    /** Fast-score bands, used when nothing was deep-evaluated. */
    strong: number;
    good: number;
  },
): string {
  if (o.candidates === 0) {
    return "No open roles matched your criteria this time.";
  }

  // A repeat search mostly finds roles it has seen and scored before, so "0
  // new" and "scored 0" are said plainly or left out rather than recited.
  const found =
    `Found ${roles(o.candidates)}` +
    (o.newMatches >= o.candidates
      ? ""
      : o.newMatches === 0
        ? " (none new)"
        : ` (${o.newMatches} new)`);
  const done = [
    found,
    o.scored > 0 && `scored ${o.scored}`,
    o.evaluated > 0 && `deep-evaluated ${o.evaluated}`,
  ]
    .filter(Boolean)
    .join(", ");

  if (o.evaluated > 0) {
    const verdict =
      o.recommended === 0
        ? "None looked worth applying to."
        : `${o.recommended} ${o.recommended === 1 ? "looks" : "look"} worth applying to` +
          (o.shortlisted > 0 ? `; ${o.shortlisted} shortlisted.` : ".");
    return `${done}. ${verdict}`;
  }

  const bands = [
    o.strong > 0 && `${o.strong} ${o.strong === 1 ? "looks" : "look"} strong`,
    o.good > 0 && `${o.good} ${o.good === 1 ? "is" : "are"} worth a look`,
    o.autoArchived > 0 &&
      `${o.autoArchived} ${o.autoArchived === 1 ? "was" : "were"} archived as too weak`,
  ].filter(Boolean);

  return `${done}.` + (bands.length ? ` ${capitalize(bands.join(", "))}.` : "");
}

/** Fails a run and says why, unless it was cancelled first. */
export async function failRun(
  ref: Pick<Run, "id" | "userId">,
  error: unknown,
): Promise<void> {
  const message = errorMessage(error);
  console.error(`[runs] run ${ref.id} failed:`, error);

  const failed = await finishActiveRun(ref.id, { status: "failed", error: message });
  if (!failed) return;

  await emit({
    userId: ref.userId,
    runId: ref.id,
    kind: "run.failed",
    level: "error",
    message,
  });
}

// --- Gate 1's continuation: deep evaluation -------------------------------------

/**
 * `search.evaluate`: the user (or autopilot) answered the deep-evaluation
 * gate. A yes evaluates the approved matches and raises the shortlist gate; a
 * no — or an approval that lapsed — completes the run without evaluating.
 */
export async function continueSearchEvaluate(
  payload: SearchContinuationPayload,
): Promise<void> {
  const { userId, runId, approvalId } = payload;

  const run = await getRun(userId, runId);
  if (!run) {
    console.error(`[evaluate] no run ${runId}`);
    return;
  }
  // Cancelled, or already finished by an earlier delivery of this job.
  if (!RESUMABLE.includes(run.status)) return;

  try {
    const approval = await getApproval(userId, approvalId);
    if (!approval || approval.runId !== run.id) {
      throw new Error("The approval this search was waiting on no longer exists.");
    }
    if (approval.status === "pending") {
      console.warn(`[evaluate] approval ${approvalId} is still pending; ignoring`);
      return;
    }

    if (!isApproved(approval)) {
      await emit({
        userId,
        runId,
        kind: "evaluation.skipped",
        message:
          approval.status === "expired"
            ? "The deep evaluation was never answered, so it was skipped."
            : "Skipped the deep evaluation, as you asked.",
        approvalId,
      });
      await mergeStats(run.id, { evaluated: 0 });
      await completeSearchRun(run);
      return;
    }

    if (!(await resumeFrom(run.id, approvalId, { phase: "evaluating" }))) return;

    const batch = approvedItemIds(approval);

    const candidate = await loadEvalCandidate(userId, {
      brief: parseBrief(asRecord(run.input).brief),
    });
    if (!candidate) {
      await emit({
        userId,
        runId,
        kind: "evaluation.skipped",
        level: "warn",
        message:
          "Skipped the deep evaluation: there is no resume on file to judge against.",
      });
      await completeSearchRun(run);
      return;
    }

    // Anything evaluated since the gate was raised (on demand, from the job
    // page) is not paid for twice, and nothing the user has since rejected is
    // paid for at all.
    const toEvaluate = await matchesNeedingEvaluation(userId, batch);

    await mergeStats(run.id, {
      toEvaluate: toEvaluate.length,
      evaluated: 0,
      evaluationFailed: 0,
    });

    let evaluatedNow = 0;

    if (toEvaluate.length > 0) {
      await emit({
        userId,
        runId,
        kind: "evaluation.start",
        message: `Deep-evaluating ${roles(toEvaluate.length)} against your resume`,
      });

      const outcome = await evaluateBatch({
        userId,
        runId,
        matchIds: toEvaluate,
        candidate,
      });

      await mergeStats(run.id, {
        evaluated: outcome.evaluated.length,
        evaluationFailed: outcome.failed.length,
      });
      evaluatedNow = outcome.evaluated.length;

      if (outcome.cancelled) {
        await reportCancelled(run, evaluatedNow);
        return;
      }
      if (evaluatedNow === 0) {
        throw new Error(
          `Deep evaluation failed for every role: ${outcome.failed[0]?.error ?? "unknown error"}`,
        );
      }
      if (outcome.failed.length > 0) {
        await emit({
          userId,
          runId,
          kind: "evaluation.failed",
          level: "warn",
          message:
            `${roles(outcome.failed.length)} could not be evaluated and ` +
            `${outcome.failed.length === 1 ? "was" : "were"} left as ${outcome.failed.length === 1 ? "it was" : "they were"}: ` +
            outcome.failed[0].error,
          data: { failed: outcome.failed },
        });
      }
    }

    // The whole approved batch, including anything evaluated earlier: that is
    // what the shortlist is proposed from.
    const rows = await getMatchRows(userId, batch.filter(isUuid));
    const recommended = rows.filter(
      (row) => row.recommendation === "strong" || row.recommendation === "apply",
    ).length;

    await mergeStats(run.id, { recommended });
    await emit({
      userId,
      runId,
      kind: "evaluation.done",
      message: describeVerdicts(rows),
    });

    if (await isCancelled(run.id)) {
      await reportCancelled(run, evaluatedNow);
      return;
    }

    await proposeShortlist(run, rows);
  } catch (error) {
    await failRun(run, error);
  }
}

/**
 * Gate 2: evaluated matches worth applying to that are still in `found`. In
 * `approve_each` this pauses the run; on autopilot it shortlists them straight
 * away. Nothing qualifying completes the run.
 */
async function proposeShortlist(run: Run, rows: MatchRow[]): Promise<void> {
  // getMatchRows returns best first, so the proposal is ordered too.
  const picks = rows.filter(isShortlistCandidate);
  if (picks.length === 0) {
    await completeSearchRun(run);
    return;
  }

  const n = picks.length;
  const payload: ShortlistPayload = {
    items: picks.map((row) => ({
      matchId: row.matchId,
      jobId: row.jobId,
      title: row.title,
      company: row.company,
      deepScore: row.deepScore,
      recommendation: (row.recommendation ?? "apply") as Recommendation,
      summary: row.headline ?? "",
    })),
  };

  await requestApproval({
    userId: run.userId,
    runId: run.id,
    kind: "shortlist_proposal",
    title: `Shortlist ${roles(n)}`,
    payload,
    mode: run.mode,
    continuation: (approvalId) => ({
      queue: QUEUES.searchShortlist,
      payload: { userId: run.userId, runId: run.id, approvalId },
    }),
    stats:
      run.mode === "autopilot"
        ? { proposedShortlist: n }
        : { phase: "awaiting_shortlist", proposedShortlist: n },
    messages: {
      requested:
        `${n === 1 ? "1 role looks" : `${n} roles look`} worth applying to. ` +
        `Shortlist ${n === 1 ? "it" : "them"}?`,
      auto: `Autopilot: shortlisting ${roles(n)} that ${n === 1 ? "looks" : "look"} worth applying to.`,
    },
    data: { count: n },
  });
}

// --- Gate 2's continuation: shortlisting ---------------------------------------

/**
 * `search.shortlist`: moves the approved matches that are still in `found` to
 * `shortlisted` — one conditional write, so a match the user moved in the
 * meantime stays where they put it — and completes the run.
 */
export async function continueSearchShortlist(
  payload: SearchContinuationPayload,
): Promise<void> {
  const { userId, runId, approvalId } = payload;

  const run = await getRun(userId, runId);
  if (!run) {
    console.error(`[shortlist] no run ${runId}`);
    return;
  }
  if (!RESUMABLE.includes(run.status)) return;

  try {
    const approval = await getApproval(userId, approvalId);
    if (!approval || approval.runId !== run.id) {
      throw new Error("The approval this search was waiting on no longer exists.");
    }
    if (approval.status === "pending") {
      console.warn(`[shortlist] approval ${approvalId} is still pending; ignoring`);
      return;
    }

    if (!isApproved(approval)) {
      await emit({
        userId,
        runId,
        kind: "shortlist.skipped",
        message:
          approval.status === "expired"
            ? "The shortlist proposal was never answered, so nothing was moved."
            : "Left the shortlist as it was, as you asked.",
        approvalId,
      });
      await mergeStats(run.id, { shortlisted: 0 });
      await completeSearchRun(run);
      return;
    }

    if (!(await resumeFrom(run.id, approvalId))) return;

    const moved = await moveMatchesFromStage(
      userId,
      approvedItemIds(approval),
      "found",
      "shortlisted",
    );

    await mergeStats(run.id, { shortlisted: moved.length });
    await emit({
      userId,
      runId,
      kind: "shortlist.done",
      message:
        moved.length > 0
          ? `Shortlisted ${roles(moved.length)}.`
          : "Nothing to shortlist: those roles had already been moved.",
      data: { matchIds: moved },
    });

    await completeSearchRun(run);
  } catch (error) {
    await failRun(run, error);
  }
}

// --- On demand ------------------------------------------------------------------

export class UnknownMatchError extends Error {
  constructor() {
    super("There is no such match to evaluate.");
    this.name = "UnknownMatchError";
  }
}

/**
 * Starts a `rank_deep` run for specific matches — the job page's "Evaluate"
 * and chat's `evaluate_job`. No approval gate: the user asked for exactly
 * this. Only this user's matches are kept; asking for one that is already
 * being evaluated returns that run rather than paying twice.
 *
 * Throws NoResumeError when there is no resume to evaluate against, and
 * UnknownMatchError when none of the ids is one of this user's matches.
 */
export async function startMatchEvaluation(
  userId: string,
  matchIds: string[],
  options: { conversationId?: string | null } = {},
): Promise<{ run: Run; reused: boolean; matches: MatchRow[] }> {
  const matches = await getMatchRows(
    userId,
    [...new Set(matchIds)].filter(isUuid),
  );
  if (matches.length === 0) throw new UnknownMatchError();

  if (!(await loadBaseResume(userId))) throw new NoResumeError();

  const ids = matches.map((match) => match.matchId).sort();

  const [existing] = await db
    .select()
    .from(runs)
    .where(
      and(
        eq(runs.userId, userId),
        eq(runs.kind, "rank_deep"),
        inArray(runs.status, ["queued", "running"]),
        sql`${runs.input} -> 'matchIds' = ${JSON.stringify(ids)}::jsonb`,
      ),
    )
    .limit(1);
  if (existing) return { run: existing, reused: true, matches };

  const run = await createRun({
    userId,
    kind: "rank_deep",
    status: "queued",
    input: { matchIds: ids },
    conversationId: options.conversationId ?? null,
  });

  try {
    await send(QUEUES.matchEvaluate, { userId, runId: run.id, matchIds: ids });
  } catch (error) {
    await finishActiveRun(run.id, {
      status: "failed",
      error: "Could not queue the evaluation.",
    });
    throw error;
  }

  return { run, reused: false, matches };
}

/** `match.evaluate`: the worker half of `startMatchEvaluation`. */
export async function executeMatchEvaluate(
  payload: MatchEvaluatePayload,
): Promise<void> {
  const { userId, runId } = payload;

  const run = await getRun(userId, runId);
  if (!run) {
    console.error(`[evaluate] no run ${runId}`);
    return;
  }
  // Cancelled before the worker got to it, or a repeat delivery.
  if (!(await setRunStatusIf(run.id, ["queued"], "running", { phase: "evaluating" }))) {
    return;
  }

  try {
    const rows = await getMatchRows(
      userId,
      [...new Set(payload.matchIds)].filter(isUuid),
    );
    if (rows.length === 0) {
      throw new Error("There is nothing to evaluate: those matches do not exist.");
    }

    await mergeStats(run.id, {
      toEvaluate: rows.length,
      evaluated: 0,
      evaluationFailed: 0,
    });
    await emit({
      userId,
      runId,
      kind: "run.started",
      message:
        rows.length === 1
          ? `Evaluating ${rows[0].title} at ${rows[0].company}`
          : `Deep-evaluating ${roles(rows.length)}`,
    });

    const candidate = await loadEvalCandidate(userId);
    if (!candidate) throw new NoResumeError();

    const outcome = await evaluateBatch({
      userId,
      runId,
      matchIds: rows.map((row) => row.matchId),
      candidate,
    });

    await mergeStats(run.id, {
      evaluated: outcome.evaluated.length,
      evaluationFailed: outcome.failed.length,
    });

    if (outcome.cancelled) {
      await reportCancelled(run, outcome.evaluated.length);
      return;
    }
    if (outcome.evaluated.length === 0) {
      throw new Error(outcome.failed[0]?.error ?? "Nothing was evaluated.");
    }

    const output: MatchEvaluateOutput = {
      evaluated: outcome.evaluated.length,
      matchIds: outcome.evaluated.map((item) => item.matchId),
    };

    const finished = await finishActiveRun(
      run.id,
      { status: "completed", output },
      { phase: "done" },
    );
    if (!finished) return;

    await emit({
      userId,
      runId,
      kind: "run.completed",
      message: describeEvaluations(outcome.evaluated, outcome.failed.length),
      data: output,
    });
  } catch (error) {
    await failRun(run, error);
  }
}

// --- The batch -------------------------------------------------------------------

type BatchOutcome = {
  evaluated: MatchEvaluation[];
  failed: Array<{ matchId: string; error: string }>;
  cancelled: boolean;
};

/**
 * Evaluates matches with bounded concurrency, checking for cancellation
 * before each one. A failed evaluation leaves its match unevaluated and the
 * batch carries on; results already written are kept whatever happens next.
 */
async function evaluateBatch(input: {
  userId: string;
  runId: string;
  matchIds: string[];
  candidate: EvalCandidate;
}): Promise<BatchOutcome> {
  const { userId, runId, matchIds, candidate } = input;
  const outcome: BatchOutcome = { evaluated: [], failed: [], cancelled: false };
  const total = matchIds.length;

  // Progress writes are chained so they land in order: several evaluations
  // finishing together would otherwise race their counters backwards.
  let progress: Promise<void> = Promise.resolve();
  let reported = 0;

  const report = () => {
    const done = outcome.evaluated.length + outcome.failed.length;
    if (done - reported < PROGRESS_EVERY && done < total) return;
    reported = done;

    const snapshot = {
      evaluated: outcome.evaluated.length,
      evaluationFailed: outcome.failed.length,
    };
    progress = progress
      .then(async () => {
        await mergeStats(runId, snapshot);
        await emit({
          userId,
          runId,
          kind: "evaluation.progress",
          level: "debug",
          message: `Evaluated ${done} of ${total}`,
        });
      })
      .catch((error) => console.error("[evaluate] progress write failed:", error));
  };

  await pooled(
    matchIds,
    async (matchId) => {
      if (outcome.cancelled) return;
      if (await isCancelled(runId)) {
        outcome.cancelled = true;
        return;
      }

      try {
        outcome.evaluated.push(
          await evaluateMatch({
            userId,
            matchId,
            candidate,
            context: { userId, runId },
          }),
        );
      } catch (error) {
        console.warn(`[evaluate] match ${matchId} failed:`, error);
        outcome.failed.push({ matchId, error: errorMessage(error) });
      }

      report();
    },
    EVAL_CONCURRENCY,
  );

  await progress;
  return outcome;
}

// --- Wording ---------------------------------------------------------------------

const VERDICT: Record<Recommendation, string> = {
  strong: "a strong match",
  apply: "worth applying",
  maybe: "a maybe",
  skip: "probably not worth applying",
};

/**
 * The closing line of an on-demand evaluation, naming the role(s):
 * "Evaluated Senior Backend Engineer at Stripe: 82, worth applying."
 */
export function describeEvaluations(
  evaluations: MatchEvaluation[],
  failed = 0,
): string {
  const label = (item: MatchEvaluation) => `${item.title} at ${item.company}`;
  const verdict = (item: MatchEvaluation) =>
    `${item.evaluation.score}, ${VERDICT[item.evaluation.recommendation]}`;

  let message: string;
  if (evaluations.length === 1) {
    const [only] = evaluations;
    message = `Evaluated ${label(only)}: ${verdict(only)}.`;
  } else {
    const best = [...evaluations].sort(
      (a, b) => b.evaluation.score - a.evaluation.score,
    );
    const shown = best.slice(0, 3).map((item) => `${label(item)} (${verdict(item)})`);
    const more = best.length > 3 ? `; and ${best.length - 3} more` : "";
    message = `Evaluated ${roles(evaluations.length)}: ${shown.join("; ")}${more}.`;
  }

  if (failed > 0) {
    message += ` ${failed} could not be evaluated.`;
  }
  return message;
}

/** "Deep evaluation done: 3 strong, 2 worth applying, 5 maybes, 2 to skip." */
function describeVerdicts(rows: MatchRow[]): string {
  const counts = { strong: 0, apply: 0, maybe: 0, skip: 0 };
  for (const row of rows) {
    if (row.recommendation) counts[row.recommendation] += 1;
  }

  const parts = [
    counts.strong > 0 && `${counts.strong} strong`,
    counts.apply > 0 && `${counts.apply} worth applying`,
    counts.maybe > 0 && `${counts.maybe} ${counts.maybe === 1 ? "maybe" : "maybes"}`,
    counts.skip > 0 && `${counts.skip} to skip`,
  ].filter(Boolean);

  return parts.length
    ? `Deep evaluation done: ${parts.join(", ")}.`
    : "Deep evaluation done.";
}

async function reportCancelled(run: Pick<Run, "id" | "userId">, kept: number) {
  await emit({
    userId: run.userId,
    runId: run.id,
    kind: "run.cancelled",
    level: "warn",
    message:
      kept > 0
        ? `Stopped. The ${roles(kept)} already evaluated ${kept === 1 ? "was" : "were"} kept.`
        : "Stopped before anything was evaluated.",
  });
}

// --- Internals -------------------------------------------------------------------

/**
 * Resumes a paused run from one particular approval, at most once.
 *
 * pg-boss can in rare cases deliver a job twice; a second delivery of the same
 * continuation would otherwise re-run its stage and raise a duplicate gate.
 * Recording the approval being resumed from, in the same conditional write
 * that sets the run running, makes the resume idempotent — and a cancel that
 * landed first still wins.
 */
async function resumeFrom(
  runId: string,
  approvalId: string,
  stats: Record<string, unknown> = {},
): Promise<boolean> {
  const [row] = await db
    .update(runs)
    .set({
      status: "running",
      stats: sql`${runs.stats} || ${JSON.stringify({ ...stats, resumedFrom: approvalId })}::jsonb`,
      updatedAt: new Date(),
    })
    .where(
      and(
        eq(runs.id, runId),
        inArray(runs.status, RESUMABLE),
        sql`(${runs.stats} ->> 'resumedFrom') is distinct from ${approvalId}`,
      ),
    )
    .returning({ id: runs.id });
  return Boolean(row);
}

function candidateJobIdsOf(run: Run): string[] {
  const ids = asRecord(run.input).candidateJobIds;
  return Array.isArray(ids)
    ? ids.filter((id): id is string => typeof id === "string")
    : [];
}

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : {};
}

function roles(n: number) {
  return `${n} ${n === 1 ? "role" : "roles"}`;
}

function capitalize(text: string) {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function isUuid(id: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id);
}
