import { and, eq, inArray, sql } from "drizzle-orm";

import { refreshCompanies } from "@/lib/companies/refresh";
import { registerBoardsFromApplyLinks } from "@/lib/companies/discovery";
import { db } from "@/lib/db";
import { staleCompanies } from "@/lib/db/queries/companies";
import { jobMatches, jobs, runs, type Run } from "@/lib/db/schema";
import { getBaseResume, getProfile } from "@/lib/db/queries/profile";
import { scoreMatches, scoreSummary } from "@/lib/ranking/fast-score";
import { ResumeSchema } from "@/lib/resume/schema";
import {
  EMPTY_PREFERENCES,
  PreferencesSchema,
  interviewProgress,
  InterviewStateSchema,
  EMPTY_INTERVIEW_STATE,
  type Preferences,
} from "@/lib/profile/interview";
import {
  deriveCriteria,
  prefilterJobs,
  type DerivedCriteria,
} from "@/lib/ranking/prefilter";
import { ingestJobs } from "@/lib/sources/ingest";
import {
  JSearchDisabledError,
  JSearchQuotaError,
  searchJSearch,
} from "@/lib/sources/jsearch";

import { emit } from "./events";
import { finishRun, isCancelled, mergeStats, startRun } from "./runs";

/**
 * One search run: refresh, aggregate, filter, record.
 *
 * The shape that matters is that this is a *pipeline with checkpoints*, not an
 * agent loop. Each stage writes its result and emits an event before the next
 * begins, so a run that is cancelled or crashes halfway has still done — and
 * kept — everything up to that point. The user watching the activity feed sees
 * the same stages.
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

  await startRun(run.id);
  await emit({
    userId,
    runId: run.id,
    kind: "run.started",
    message: input.prompt
      ? `Searching: ${input.prompt}`
      : "Searching for matching roles",
  });

  try {
    const preferences = await assertCanSearch(userId);

    // 1. Criteria — cached on the run so a resume does not pay twice.
    const criteria =
      input.criteria ??
      (await deriveCriteria(preferences, { userId, runId: run.id }));

    await db
      .update(runs)
      .set({ input: { ...input, criteria }, updatedAt: new Date() })
      .where(eq(runs.id, run.id));

    await emit({
      userId,
      runId: run.id,
      kind: "search.criteria",
      message: `Looking for titles like: ${criteria.title_include.slice(0, 6).join(", ")}`,
      data: criteria,
    });

    if (await isCancelled(run.id)) return void (await cancelled(run, userId));

    // 2. Refresh stale boards, so the search is over current postings.
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
    await runAggregator(run, preferences, criteria);

    if (await isCancelled(run.id)) return void (await cancelled(run, userId));

    // 4. Narrow the mirror to what is worth scoring.
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

    // 5. Record them as matches. Scoring is Step 16's job.
    const created = await upsertMatches(userId, prefilter.jobIds);

    await mergeStats(run.id, {
      matches: prefilter.jobIds.length,
      newMatches: created,
    });

    if (await isCancelled(run.id)) return void (await cancelled(run, userId));

    // 6. Score them. Cheap tier, one call per posting, triage not judgement.
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

    const summary = await scoreSummary(userId);

    await mergeStats(run.id, {
      scored: scoring.scored,
      autoArchived: scoring.archived,
      scoringFailed: scoring.failed,
    });

    await finishRun(run.id, {
      status: "completed",
      output: {
        candidates: prefilter.jobIds.length,
        newMatches: created,
        scored: scoring.scored,
        autoArchived: scoring.archived,
        strong: summary.strong,
      },
    });

    await emit({
      userId,
      runId: run.id,
      kind: "run.completed",
      message:
        `Found ${prefilter.jobIds.length} roles, scored ${scoring.scored}. ` +
        `${summary.strong} look strong, ${summary.good} are worth a look, ` +
        `${scoring.archived} were archived as too weak.`,
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);

    await finishRun(run.id, { status: "failed", error: message });
    await emit({
      userId,
      runId: run.id,
      kind: "run.failed",
      level: "error",
      message,
    });
  }
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

/** Matches with their job, for the chat tools and the jobs list. */
export async function listMatches(
  userId: string,
  options: { limit?: number; stage?: (typeof jobMatches.stage.enumValues)[number] } = {},
) {
  const conditions = [eq(jobMatches.userId, userId)];
  if (options.stage) conditions.push(eq(jobMatches.stage, options.stage));

  return db
    .select({
      matchId: jobMatches.id,
      stage: jobMatches.stage,
      fastScore: jobMatches.fastScore,
      deepScore: jobMatches.deepScore,
      jobId: jobs.id,
      title: jobs.title,
      company: jobs.companyName,
      locations: jobs.locations,
      remote: jobs.remote,
      url: jobs.url,
      salaryMin: jobs.salaryMin,
      salaryMax: jobs.salaryMax,
      postedAt: jobs.postedAt,
    })
    .from(jobMatches)
    .innerJoin(jobs, eq(jobs.id, jobMatches.jobId))
    .where(and(...conditions))
    .orderBy(
      sql`${jobMatches.deepScore} desc nulls last, ${jobMatches.fastScore} desc nulls last, ${jobs.postedAt} desc nulls last`,
    )
    .limit(options.limit ?? 25);
}
