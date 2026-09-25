import { and, eq, inArray, sql } from "drizzle-orm";

import { refreshCompanies } from "@/lib/companies/refresh";
import { registerBoardsFromApplyLinks } from "@/lib/companies/discovery";
import { db } from "@/lib/db";
import { staleCompanies } from "@/lib/db/queries/companies";
import { selectForDeepEval } from "@/lib/db/queries/matches";
import { jobMatches, runs, type Run } from "@/lib/db/schema";
import { getBaseResume, getProfile } from "@/lib/db/queries/profile";
import {
  costPhrase,
  estimateEvalCostFor,
  loadEvalCandidate,
} from "@/lib/ranking/deep-eval";
import { scoreMatches } from "@/lib/ranking/fast-score";
import { ResumeSchema } from "@/lib/resume/schema";
import {
  EMPTY_PREFERENCES,
  PreferencesSchema,
  interviewProgress,
  InterviewStateSchema,
  EMPTY_INTERVIEW_STATE,
  type Preferences,
} from "@/lib/profile/interview";
import { getSettings, type Settings } from "@/lib/profile/settings";
import {
  deriveCriteria,
  prefilterJobs,
  type DerivedCriteria,
} from "@/lib/ranking/prefilter";
import { QUEUES } from "@/lib/queue/queues";
import { ingestJobs } from "@/lib/sources/ingest";
import {
  JSearchDisabledError,
  JSearchQuotaError,
  searchJSearch,
} from "@/lib/sources/jsearch";

import { requestApproval, type DeepEvalBatchPayload } from "./approvals";
import { completeSearchRun, failRun } from "./evaluate-run";
import { emit } from "./events";
import { isCancelled, mergeStats, setRunStatusIf } from "./runs";

/**
 * One search run: refresh, aggregate, filter, record, score — then the gates.
 *
 * The shape that matters is that this is a *pipeline with checkpoints*, not an
 * agent loop. Each stage writes its result and emits an event before the next
 * begins, so a run that is cancelled or crashes halfway has still done — and
 * kept — everything up to that point. The user watching the activity feed sees
 * the same stages, named in `stats.phase`: starting → refreshing →
 * aggregating → filtering → scoring → awaiting_evaluation → evaluating →
 * awaiting_shortlist → done.
 *
 * This handler ends at the first gate. Deep evaluation costs real money, so in
 * `approve_each` the run pauses there and returns; the decision enqueues the
 * continuation in evaluate-run.ts, which evaluates, raises the shortlist gate,
 * and eventually completes the run. On autopilot the same gate approves itself.
 *
 * Cancellation is checked between stages rather than inside them: stopping
 * mid-write would leave matches half-recorded, and no stage here runs long
 * enough for that to feel unresponsive.
 */

/** Boards not fetched within this window are refreshed before searching. */
const STALE_AFTER_MS = 2 * 60 * 60 * 1000;

/**
 * Aggregator pages per run. Each is one request against a 200/month budget, so
 * this is deliberately far below what the plan allowed — a handful of pages is
 * enough to surface companies we do not already track, which is the only thing
 * the aggregator is for once the registry is large.
 */
const MAX_JSEARCH_REQUESTS = 3;

export type SearchRunInput = {
  /** What the user asked for, verbatim, for the record. */
  prompt?: string | null;
  /** Cached so a resumed run does not pay to derive them twice. */
  criteria?: DerivedCriteria;
  skipRefresh?: boolean;
  /**
   * The postings this run's prefilter chose. The continuations after the gates
   * run long after this handler has returned, and this is what "this run's
   * matches" means to them.
   */
  candidateJobIds?: string[];
};

export class PreferencesIncompleteError extends Error {
  constructor(readonly missing: string[]) {
    super(`Cannot search yet: ${missing.join(", ")} not known`);
    this.name = "PreferencesIncompleteError";
  }
}

/**
 * The gate on starting a search.
 *
 * Searching without knowing work authorisation or location produces a list that
 * looks plausible and is mostly unusable, which is worse than refusing — the
 * user cannot tell the difference by looking.
 */
export async function assertCanSearch(userId: string): Promise<Preferences> {
  const profile = await getProfile(userId);
  const preferences =
    PreferencesSchema.safeParse(profile?.preferences).data ?? EMPTY_PREFERENCES;
  const state =
    InterviewStateSchema.safeParse(profile?.interviewState).data ??
    EMPTY_INTERVIEW_STATE;

  const progress = interviewProgress(preferences, state);
  if (progress.blocking.length > 0) {
    throw new PreferencesIncompleteError(
      progress.blocking.map((slot) => slot.label),
    );
  }

  return preferences;
}

export async function executeSearchRun(run: Run): Promise<void> {
  const userId = run.userId;
  const input = (run.input ?? {}) as SearchRunInput;

  // Conditional, so a cancel that landed while the job was queued stands.
  const started = await setRunStatusIf(run.id, ["queued", "running"], "running", {
    phase: "starting",
  });
  if (!started) return;

  await emit({
    userId,
    runId: run.id,
    kind: "run.started",
    message: input.prompt
      ? `Searching: ${input.prompt}`
      : "Searching for matching roles",
  });

  try {
    const settings = await getSettings(userId);
    const preferences = await assertCanSearch(userId);

    // 1. Criteria — cached on the run so a resume does not pay twice.
    const criteria =
      input.criteria ??
      (await deriveCriteria(preferences, { userId, runId: run.id }));

    await saveInput(run.id, { criteria });

    await emit({
      userId,
      runId: run.id,
      kind: "search.criteria",
      message: `Looking for titles like: ${criteria.title_include.slice(0, 6).join(", ")}`,
      data: criteria,
    });

    if (await isCancelled(run.id)) return void (await cancelled(run, userId));

    // 2. Refresh stale boards, so the search is over current postings.
    await mergeStats(run.id, { phase: "refreshing" });

    if (!input.skipRefresh) {
      const stale = await staleCompanies(new Date(Date.now() - STALE_AFTER_MS));
      if (stale.length > 0) {
        await emit({
          userId,
          runId: run.id,
          kind: "source.refresh.start",
          message: `Refreshing ${stale.length} stale job boards`,
        });

        const outcomes = await refreshCompanies(stale);
        const ok = outcomes.filter((outcome) => outcome.status === "ok");

        await mergeStats(run.id, {
          boardsRefreshed: ok.length,
          boardsFailed: outcomes.length - ok.length,
        });
        await emit({
          userId,
          runId: run.id,
          kind: "source.refresh.done",
          message: `Refreshed ${ok.length}/${stale.length} boards`,
        });
      }
    }

    if (await isCancelled(run.id)) return void (await cancelled(run, userId));

    // 3. The aggregator, for companies we do not already track.
    await mergeStats(run.id, { phase: "aggregating" });
    await runAggregator(run, preferences, criteria);

    if (await isCancelled(run.id)) return void (await cancelled(run, userId));

    // 4. Narrow the mirror to what is worth scoring.
    await mergeStats(run.id, { phase: "filtering" });
    const prefilter = await prefilterJobs({ userId, preferences, criteria });

    await mergeStats(run.id, {
      activeInMirror: prefilter.funnel.activeInMirror,
      candidates: prefilter.funnel.selected,
    });
    await emit({
      userId,
      runId: run.id,
      kind: "prefilter.done",
      message:
        `${prefilter.funnel.selected} of ${prefilter.funnel.activeInMirror.toLocaleString()} ` +
        `open postings match your criteria` +
        (prefilter.funnel.cappedAt ? " (capped)" : ""),
      data: prefilter.funnel,
    });

    // 5. Record them as matches, and remember which postings they were.
    const created = await upsertMatches(userId, prefilter.jobIds);
    await saveInput(run.id, { candidateJobIds: prefilter.jobIds });

    await mergeStats(run.id, {
      matches: prefilter.jobIds.length,
      newMatches: created,
    });

    if (await isCancelled(run.id)) return void (await cancelled(run, userId));

    // 6. Score them. Cheap tier, one call per posting, triage not judgement.
    await mergeStats(run.id, { phase: "scoring" });

    const baseResume = await getBaseResume(userId);
    const resume = baseResume
      ? (ResumeSchema.safeParse(baseResume.data).data ?? null)
      : null;

    await emit({
      userId,
      runId: run.id,
      kind: "scoring.start",
      message: `Scoring ${prefilter.jobIds.length} roles against your profile`,
    });

    const scoring = await scoreMatches({
      userId,
      preferences,
      resume,
      jobIds: prefilter.jobIds,
      threshold: settings.scoreThreshold,
      context: { userId, runId: run.id },
      shouldStop: () => isCancelled(run.id),
      onProgress: async (done, total) => {
        await mergeStats(run.id, { scored: done });
        await emit({
          userId,
          runId: run.id,
          kind: "scoring.progress",
          level: "debug",
          message: `Scored ${done} of ${total}`,
        });
      },
    });

    await mergeStats(run.id, {
      scored: scoring.scored,
      autoArchived: scoring.archived,
      scoringFailed: scoring.failed,
    });

    if (await isCancelled(run.id)) return void (await cancelled(run, userId));

    await emit({
      userId,
      runId: run.id,
      kind: "scoring.done",
      message:
        prefilter.jobIds.length === 0
          ? "Nothing to score."
          : scoring.scored === 0 && scoring.failed === 0
          ? "Nothing new to score: every one of these was scored before."
          : `Scored ${scoring.scored} ${scoring.scored === 1 ? "role" : "roles"}` +
            (scoring.archived > 0
              ? `; ${scoring.archived} came in under ${settings.scoreThreshold} and ${scoring.archived === 1 ? "was" : "were"} archived.`
              : ".") +
            (scoring.failed > 0 ? ` ${scoring.failed} could not be scored.` : ""),
    });

    // 7. The first gate: which matches earn the strong model's time.
    await gateDeepEvaluation(run, settings, prefilter.jobIds);
  } catch (error) {
    await failRun(run, error);
  }
}

/**
 * Picks the best unevaluated matches and asks before paying to evaluate them —
 * or, on autopilot, approves that itself. Ends the run when there is nothing
 * to evaluate, deep evaluation is turned off, or it cannot run at all.
 *
 * Exported for tests; the search run calls it once scoring is done.
 */
export async function gateDeepEvaluation(
  run: Run,
  settings: Settings,
  candidateJobIds: string[],
): Promise<void> {
  const userId = run.userId;

  if (settings.deepEvalTopN <= 0) {
    await completeSearchRun(run);
    return;
  }

  const selected = await selectForDeepEval(userId, {
    minFastScore: settings.scoreThreshold,
    limit: settings.deepEvalTopN,
    preferJobIds: candidateJobIds,
  });

  if (selected.length === 0) {
    await completeSearchRun(run);
    return;
  }

  const candidate = await loadEvalCandidate(userId);
  if (!candidate) {
    await emit({
      userId,
      runId: run.id,
      kind: "evaluation.skipped",
      level: "warn",
      message:
        "Skipped the deep evaluation: there is no resume on file to judge against.",
    });
    await completeSearchRun(run);
    return;
  }

  // The quote is priced from the prompts that would actually be sent.
  const estimate = estimateEvalCostFor(
    candidate,
    selected.map((row) => row.descriptionChars),
  );
  if (estimate.model === "unconfigured") {
    await emit({
      userId,
      runId: run.id,
      kind: "evaluation.skipped",
      level: "warn",
      message:
        "Skipped the deep evaluation: no model is configured for it (LLM_EVALUATE or LLM_DEFAULT).",
    });
    await completeSearchRun(run);
    return;
  }

  const n = selected.length;
  const cost = costPhrase(estimate.usd);
  const priced = cost ? ` (${cost})` : "";
  const earlier = selected.filter((row) => !row.preferred).length;

  const notes = [
    earlier > 0 &&
      `${earlier === n ? "All" : earlier} of these ${earlier === 1 ? "comes" : "come"} from earlier searches and ${earlier === 1 ? "was" : "were"} never deep-evaluated.`,
    estimate.usd == null &&
      `${estimate.model} has no price on file, so there is no cost estimate.`,
  ].filter((note): note is string => Boolean(note));

  const payload: DeepEvalBatchPayload = {
    items: selected.map((row) => ({
      matchId: row.matchId,
      jobId: row.jobId,
      title: row.title,
      company: row.company,
      fastScore: row.fastScore,
    })),
    estimate,
    note: notes.length ? notes.join(" ") : null,
  };

  const what = `${n} ${n === 1 ? "role" : "roles"}`;

  await requestApproval({
    userId,
    runId: run.id,
    kind: "deep_eval_batch",
    title: `Deep-evaluate ${what}${priced}`,
    payload,
    mode: run.mode,
    continuation: (approvalId) => ({
      queue: QUEUES.searchEvaluate,
      payload: { userId, runId: run.id, approvalId },
    }),
    stats:
      run.mode === "autopilot"
        ? { phase: "evaluating", toEvaluate: n }
        : { phase: "awaiting_evaluation", toEvaluate: n },
    messages: {
      requested: `Waiting for your go-ahead to deep-evaluate ${what}${priced}.`,
      auto: `Autopilot: deep-evaluating ${what}${priced}.`,
    },
    data: { count: n, estimate },
  });
}

async function cancelled(run: Run, userId: string) {
  await emit({
    userId,
    runId: run.id,
    kind: "run.cancelled",
    level: "warn",
    message: "Search cancelled. Everything found so far was kept.",
  });
}

/** Merges into `runs.input`, so a later write does not drop an earlier one. */
async function saveInput(runId: string, patch: Partial<SearchRunInput>) {
  await db
    .update(runs)
    .set({
      input: sql`${runs.input} || ${JSON.stringify(patch)}::jsonb`,
      updatedAt: new Date(),
    })
    .where(eq(runs.id, runId));
}

/**
 * The aggregator pass.
 *
 * Failing here must not fail the run: the registry is the primary source, and
 * a quota that has run out is a normal state rather than an error.
 */
async function runAggregator(
  run: Run,
  preferences: Preferences,
  criteria: DerivedCriteria,
) {
  const roles = preferences.target_roles?.slice(0, 1) ?? [];
  const where = preferences.locations?.[0] ?? "";
  const query = [roles[0] ?? criteria.title_include[0] ?? "software engineer", where]
    .filter(Boolean)
    .join(" in ");

  try {
    const result = await searchJSearch({
      query,
      maxRequests: MAX_JSEARCH_REQUESTS,
      remoteOnly: preferences.remote_pref === "remote",
    });

    await ingestJobs({ jobs: result.jobs });

    const registered = await registerBoardsFromApplyLinks(
      result.discoveredBoards,
      { addedByUserId: run.userId },
    );

    await mergeStats(run.id, {
      aggregatorJobs: result.jobs.length,
      aggregatorRequests: result.requestsUsed,
      companiesDiscovered: registered.length,
    });

    await emit({
      userId: run.userId,
      runId: run.id,
      kind: "source.aggregator.done",
      message:
        `Aggregator: ${result.jobs.length} postings for "${query}"` +
        (registered.length
          ? `, ${registered.length} new job boards registered`
          : ""),
      data: { requestsThisMonth: result.requestsThisMonth },
    });
  } catch (error) {
    const expected =
      error instanceof JSearchQuotaError || error instanceof JSearchDisabledError;

    await emit({
      userId: run.userId,
      runId: run.id,
      kind: "source.aggregator.skipped",
      level: expected ? "info" : "warn",
      message: error instanceof Error ? error.message : String(error),
    });
  }
}

/**
 * Records the shortlist as `found` matches.
 *
 * Existing matches are left exactly as they are: a job the user has already
 * shortlisted or applied to must not be dragged back to `found` by a later
 * search seeing it again.
 */
async function upsertMatches(
  userId: string,
  jobIds: string[],
): Promise<number> {
  if (jobIds.length === 0) return 0;

  let created = 0;

  for (let offset = 0; offset < jobIds.length; offset += 500) {
    const batch = jobIds.slice(offset, offset + 500);

    const existing = await db
      .select({ jobId: jobMatches.jobId })
      .from(jobMatches)
      .where(
        and(eq(jobMatches.userId, userId), inArray(jobMatches.jobId, batch)),
      );

    const known = new Set(existing.map((row) => row.jobId));
    const fresh = batch.filter((id) => !known.has(id));
    if (fresh.length === 0) continue;

    await db
      .insert(jobMatches)
      .values(fresh.map((jobId) => ({ userId, jobId, stage: "found" as const })))
      .onConflictDoNothing();

    created += fresh.length;
  }

  return created;
}
