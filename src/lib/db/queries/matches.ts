import { and, desc, eq, ilike, inArray, or, sql, type SQL } from "drizzle-orm";

import { db } from "@/lib/db";
import { jobMatches, jobs, type Job, type JobMatch } from "@/lib/db/schema";
import type { DeepEval } from "@/lib/ranking/schemas";

/**
 * A user's matches: the jobs Foray found for them, where each sits on their
 * board, and what the two scoring passes concluded.
 *
 * `userId` first, always — a match is the per-user half of a shared posting,
 * and nothing here can read another user's.
 */

export type Stage = JobMatch["stage"];

export const OPEN_STAGES: Stage[] = [
  "found",
  "shortlisted",
  "applied",
  "interviewing",
  "offer",
];
export const CLOSED_STAGES: Stage[] = ["rejected", "archived"];
export const ALL_STAGES: Stage[] = [...OPEN_STAGES, ...CLOSED_STAGES];

/** The deep evaluation's verdict, when one exists. */
export type Recommendation = "strong" | "apply" | "maybe" | "skip";

/** What lists, boards and chat cards need about one match. */
export type MatchRow = {
  matchId: string;
  jobId: string;
  stage: Stage;
  stageChangedAt: Date;
  fastScore: number | null;
  deepScore: number | null;
  /** deepScore when there is one, else fastScore. What the UI ranks by. */
  score: number | null;
  recommendation: Recommendation | null;
  /** One line on why it fits: the deep summary, else the first fast reason. */
  headline: string | null;
  title: string;
  company: string;
  locations: string[];
  remote: Job["remote"];
  salaryMin: number | null;
  salaryMax: number | null;
  salaryCurrency: string | null;
  salaryPeriod: string | null;
  postedAt: Date | null;
  url: string;
  applyUrl: string | null;
  isActive: boolean;
  tailoredResumeId: string | null;
  userRating: number | null;
  createdAt: Date;
};

const score = sql<number | null>`coalesce(${jobMatches.deepScore}, ${jobMatches.fastScore})`;

const columns = {
  matchId: jobMatches.id,
  jobId: jobs.id,
  stage: jobMatches.stage,
  stageChangedAt: jobMatches.stageChangedAt,
  fastScore: jobMatches.fastScore,
  deepScore: jobMatches.deepScore,
  score,
  recommendation: sql<Recommendation | null>`${jobMatches.deepEval}->>'recommendation'`,
  headline: sql<string | null>`coalesce(${jobMatches.deepEval}->>'summary', ${jobMatches.fastEval}->'fit_reasons'->>0)`,
  title: jobs.title,
  company: jobs.companyName,
  locations: jobs.locations,
  remote: jobs.remote,
  salaryMin: sql<number | null>`${jobs.salaryMin}::float8`,
  salaryMax: sql<number | null>`${jobs.salaryMax}::float8`,
  salaryCurrency: jobs.salaryCurrency,
  salaryPeriod: jobs.salaryPeriod,
  postedAt: jobs.postedAt,
  url: jobs.url,
  applyUrl: jobs.applyUrl,
  isActive: jobs.isActive,
  tailoredResumeId: jobMatches.tailoredResumeId,
  userRating: jobMatches.userRating,
  createdAt: jobMatches.createdAt,
};

export type ListMatchesOptions = {
  stages?: Stage[];
  /** Only matches scoring at least this (deep score preferred). */
  minScore?: number;
  /** Case-insensitive match on title or company. */
  query?: string;
  /** Only matches with a deep evaluation. */
  evaluatedOnly?: boolean;
  /** Only matches for these postings, e.g. what one search run found. */
  jobIds?: string[];
  limit?: number;
  offset?: number;
};

/** Matches with their job, best first. */
export async function listMatches(
  userId: string,
  options: ListMatchesOptions = {},
): Promise<MatchRow[]> {
  const conditions: SQL[] = [eq(jobMatches.userId, userId)];

  if (options.stages?.length) {
    conditions.push(inArray(jobMatches.stage, options.stages));
  }
  if (options.minScore != null) {
    conditions.push(sql`${score} >= ${options.minScore}`);
  }
  if (options.evaluatedOnly) {
    conditions.push(sql`${jobMatches.deepEval} is not null`);
  }
  if (options.jobIds) {
    if (options.jobIds.length === 0) return [];
    conditions.push(inArray(jobMatches.jobId, options.jobIds));
  }
  if (options.query?.trim()) {
    const pattern = `%${options.query.trim()}%`;
    const text = or(ilike(jobs.title, pattern), ilike(jobs.companyName, pattern));
    if (text) conditions.push(text);
  }

  const rows = await db
    .select(columns)
    .from(jobMatches)
    .innerJoin(jobs, eq(jobs.id, jobMatches.jobId))
    .where(and(...conditions))
    .orderBy(
      sql`${score} desc nulls last`,
      sql`${jobs.postedAt} desc nulls last`,
    )
    .limit(options.limit ?? 50)
    .offset(options.offset ?? 0);

  return rows as MatchRow[];
}

export async function getMatchRows(
  userId: string,
  matchIds: string[],
): Promise<MatchRow[]> {
  if (matchIds.length === 0) return [];
  const rows = await db
    .select(columns)
    .from(jobMatches)
    .innerJoin(jobs, eq(jobs.id, jobMatches.jobId))
    .where(and(eq(jobMatches.userId, userId), inArray(jobMatches.id, matchIds)))
    .orderBy(sql`${score} desc nulls last`);
  return rows as MatchRow[];
}

/** Everything the job page shows: the match, its evaluations, the posting. */
export type MatchDetail = {
  match: JobMatch;
  job: Job;
};

export async function getMatchDetail(
  userId: string,
  matchId: string,
): Promise<MatchDetail | null> {
  if (!UUID.test(matchId)) return null;

  const [row] = await db
    .select({ match: jobMatches, job: jobs })
    .from(jobMatches)
    .innerJoin(jobs, eq(jobs.id, jobMatches.jobId))
    .where(and(eq(jobMatches.userId, userId), eq(jobMatches.id, matchId)))
    .limit(1);

  return row ?? null;
}

/** The user's match for a posting, if Foray has matched them to it. */
export async function getMatchByJob(
  userId: string,
  jobId: string,
): Promise<JobMatch | null> {
  const [row] = await db
    .select()
    .from(jobMatches)
    .where(and(eq(jobMatches.userId, userId), eq(jobMatches.jobId, jobId)))
    .limit(1);
  return row ?? null;
}

/**
 * Accepts either a match id or a job id — the chat agent sees both, and
 * asking it to keep them apart is a failure mode rather than a feature.
 */
export async function resolveMatch(
  userId: string,
  matchOrJobId: string,
): Promise<JobMatch | null> {
  if (UUID.test(matchOrJobId)) {
    const [row] = await db
      .select()
      .from(jobMatches)
      .where(
        and(eq(jobMatches.userId, userId), eq(jobMatches.id, matchOrJobId)),
      )
      .limit(1);
    if (row) return row;
  }
  return getMatchByJob(userId, matchOrJobId);
}

/**
 * Creates the match if it does not exist — for a posting the user found
 * themselves (from chat) rather than one a search surfaced.
 */
export async function ensureMatch(
  userId: string,
  jobId: string,
): Promise<JobMatch> {
  const existing = await getMatchByJob(userId, jobId);
  if (existing) return existing;

  const [created] = await db
    .insert(jobMatches)
    .values({ userId, jobId, stage: "found" })
    .onConflictDoNothing()
    .returning();
  return created ?? (await getMatchByJob(userId, jobId))!;
}

/**
 * `resolveMatch`, falling back to creating the match when the id is a posting
 * the user has no match for yet. Null when it is neither. For chat, where a
 * job the user found themselves is as valid a target as one a search found.
 */
export async function resolveOrCreateMatch(
  userId: string,
  matchOrJobId: string,
): Promise<JobMatch | null> {
  const existing = await resolveMatch(userId, matchOrJobId);
  if (existing) return existing;

  const [job] = await db
    .select({ id: jobs.id })
    .from(jobs)
    .where(eq(jobs.id, matchOrJobId))
    .limit(1);
  return job ? ensureMatch(userId, job.id) : null;
}

export async function countByStage(
  userId: string,
): Promise<Record<Stage, number>> {
  const rows = await db
    .select({ stage: jobMatches.stage, count: sql<number>`count(*)::int` })
    .from(jobMatches)
    .where(eq(jobMatches.userId, userId))
    .groupBy(jobMatches.stage);

  const counts = Object.fromEntries(ALL_STAGES.map((s) => [s, 0])) as Record<
    Stage,
    number
  >;
  for (const row of rows) counts[row.stage] = row.count;
  return counts;
}

/**
 * Moves a match to a stage. Returns null when the match is not this user's.
 * Moving to the stage it is already in is a no-op that still succeeds.
 */
export async function setMatchStage(
  userId: string,
  matchId: string,
  stage: Stage,
): Promise<JobMatch | null> {
  const [row] = await db
    .update(jobMatches)
    .set({
      stage,
      stageChangedAt: sql`case when ${jobMatches.stage} = ${stage} then ${jobMatches.stageChangedAt} else now() end`,
      updatedAt: new Date(),
    })
    .where(and(eq(jobMatches.userId, userId), eq(jobMatches.id, matchId)))
    .returning();
  return row ?? null;
}

export async function updateMatch(
  userId: string,
  matchId: string,
  patch: { notes?: string | null; userRating?: number | null },
): Promise<JobMatch | null> {
  const [row] = await db
    .update(jobMatches)
    .set({
      ...(patch.notes !== undefined ? { notes: patch.notes } : {}),
      ...(patch.userRating !== undefined ? { userRating: patch.userRating } : {}),
      updatedAt: new Date(),
    })
    .where(and(eq(jobMatches.userId, userId), eq(jobMatches.id, matchId)))
    .returning();
  return row ?? null;
}

/** New matches since a moment — the dashboard's "since you were last here". */
export async function countNewSince(userId: string, since: Date) {
  const [row] = await db
    .select({
      total: sql<number>`count(*)::int`,
      strong: sql<number>`count(*) filter (where ${score} >= 80)::int`,
    })
    .from(jobMatches)
    .where(
      and(
        eq(jobMatches.userId, userId),
        sql`${jobMatches.createdAt} >= ${since}`,
        inArray(jobMatches.stage, OPEN_STAGES),
      ),
    );
  return row ?? { total: 0, strong: 0 };
}

export async function latestMatchedAt(userId: string): Promise<Date | null> {
  const [row] = await db
    .select({ at: jobMatches.createdAt })
    .from(jobMatches)
    .where(eq(jobMatches.userId, userId))
    .orderBy(desc(jobMatches.createdAt))
    .limit(1);
  return row?.at ?? null;
}

// --- Deep evaluation and shortlisting ------------------------------------------

/** A match the deep evaluation could run on, with what its cost estimate needs. */
export type DeepEvalCandidate = {
  matchId: string;
  jobId: string;
  title: string;
  company: string;
  fastScore: number | null;
  /** Length of the cleaned description — the variable part of the prompt. */
  descriptionChars: number;
};

/**
 * The matches among these postings worth the strong model's time: open (found
 * or shortlisted), fast-scored at or above the threshold, never
 * deep-evaluated, on a posting that is still live. Best fast score first.
 *
 * Limited to the postings given — a search run's own candidates. It used to
 * top the batch up with unevaluated matches from earlier searches, which put
 * senior backend roles into the review of a search for entry-level roles at
 * one company.
 */
export async function selectForDeepEval(
  userId: string,
  options: { jobIds: string[]; minFastScore: number; limit: number },
): Promise<DeepEvalCandidate[]> {
  if (options.limit <= 0 || options.jobIds.length === 0) return [];

  return db
    .select({
      matchId: jobMatches.id,
      jobId: jobs.id,
      title: jobs.title,
      company: jobs.companyName,
      fastScore: jobMatches.fastScore,
      descriptionChars: sql<number>`coalesce(length(${jobs.descriptionText}), 0)::int`,
    })
    .from(jobMatches)
    .innerJoin(jobs, eq(jobs.id, jobMatches.jobId))
    .where(
      and(
        eq(jobMatches.userId, userId),
        inArray(jobMatches.jobId, options.jobIds),
        inArray(jobMatches.stage, ["found", "shortlisted"]),
        sql`${jobMatches.fastScore} >= ${options.minFastScore}`,
        sql`${jobMatches.deepEval} is null`,
        eq(jobs.isActive, true),
      ),
    )
    .orderBy(
      sql`${jobMatches.fastScore} desc`,
      sql`${jobs.postedAt} desc nulls last`,
    )
    .limit(options.limit);
}

/**
 * Of these matches, the ones an evaluation should still be paid for: this
 * user's, not yet deep-evaluated, and not rejected or archived since they were
 * picked. Keeps the input order.
 */
export async function matchesNeedingEvaluation(
  userId: string,
  matchIds: string[],
): Promise<string[]> {
  const ids = matchIds.filter((id) => UUID.test(id));
  if (ids.length === 0) return [];

  const rows = await db
    .select({ id: jobMatches.id })
    .from(jobMatches)
    .where(
      and(
        eq(jobMatches.userId, userId),
        inArray(jobMatches.id, ids),
        sql`${jobMatches.deepEval} is null`,
        inArray(jobMatches.stage, OPEN_STAGES),
      ),
    );

  const keep = new Set(rows.map((row) => row.id));
  return ids.filter((id) => keep.has(id));
}

/**
 * The shortlist rule: deep-evaluated as worth applying to, and still sitting
 * in `found` — a match the user has already moved is theirs, not ours.
 */
export function isShortlistCandidate(
  row: Pick<MatchRow, "stage" | "recommendation">,
): boolean {
  return (
    row.stage === "found" &&
    (row.recommendation === "strong" || row.recommendation === "apply")
  );
}

/** The matches among these that the shortlist rule picks, best first. */
export async function listShortlistCandidates(
  userId: string,
  matchIds: string[],
): Promise<MatchRow[]> {
  const rows = await getMatchRows(
    userId,
    matchIds.filter((id) => UUID.test(id)),
  );
  return rows.filter(isShortlistCandidate);
}

/**
 * Moves matches to `to`, but only those still in `from`, in one statement —
 * so a match the user moved in the meantime is left where they put it.
 * Returns the ids that moved.
 */
export async function moveMatchesFromStage(
  userId: string,
  matchIds: string[],
  from: Stage,
  to: Stage,
): Promise<string[]> {
  const ids = matchIds.filter((id) => UUID.test(id));
  if (ids.length === 0) return [];

  const rows = await db
    .update(jobMatches)
    .set({ stage: to, stageChangedAt: new Date(), updatedAt: new Date() })
    .where(
      and(
        eq(jobMatches.userId, userId),
        inArray(jobMatches.id, ids),
        eq(jobMatches.stage, from),
      ),
    )
    .returning({ id: jobMatches.id });

  return rows.map((row) => row.id);
}

/**
 * The best open matches among a set of postings, deep score preferred — a
 * search run's headline results.
 */
export async function topMatchIdsForJobs(
  userId: string,
  jobIds: string[],
  limit = 5,
): Promise<string[]> {
  if (jobIds.length === 0 || limit <= 0) return [];

  const rows = await db
    .select({ id: jobMatches.id })
    .from(jobMatches)
    .where(
      and(
        eq(jobMatches.userId, userId),
        inArray(jobMatches.jobId, jobIds),
        inArray(jobMatches.stage, OPEN_STAGES),
        sql`${score} is not null`,
      ),
    )
    .orderBy(sql`${score} desc`, desc(jobMatches.updatedAt))
    .limit(limit);

  return rows.map((row) => row.id);
}

/** How a set of postings scored, for a run's summary. Open matches only. */
export async function scoreBandsForJobs(
  userId: string,
  jobIds: string[],
): Promise<{ strong: number; good: number }> {
  if (jobIds.length === 0) return { strong: 0, good: 0 };

  const [row] = await db
    .select({
      strong: sql<number>`count(*) filter (where ${score} >= 80)::int`,
      good: sql<number>`count(*) filter (where ${score} >= 65 and ${score} < 80)::int`,
    })
    .from(jobMatches)
    .where(
      and(
        eq(jobMatches.userId, userId),
        inArray(jobMatches.jobId, jobIds),
        inArray(jobMatches.stage, OPEN_STAGES),
      ),
    );

  return row ?? { strong: 0, good: 0 };
}

/**
 * Stores a deep evaluation. Returns null when the match is not this user's.
 *
 * A "skip" verdict archives the match, as a low triage score does: the review
 * read the whole posting against the resume and found a blocker, and leaving
 * it in Found buries the roles worth applying to. Only an untouched match
 * moves — one the user has already placed anywhere else stays put — and the
 * verdict stays on the match for anyone who looks in Archived.
 */
export async function saveDeepEval(
  userId: string,
  matchId: string,
  evaluation: DeepEval,
): Promise<JobMatch | null> {
  const archive =
    evaluation.recommendation === "skip"
      ? {
          stage: sql`case when ${jobMatches.stage} = 'found' then 'archived'::match_stage else ${jobMatches.stage} end`,
          stageChangedAt: sql`case when ${jobMatches.stage} = 'found' then now() else ${jobMatches.stageChangedAt} end`,
          notes: sql`case when ${jobMatches.stage} = 'found' then concat_ws(E'\n\n', nullif(${jobMatches.notes}, ''), ${"Archived: the deep review recommended skipping this one."}::text) else ${jobMatches.notes} end`,
        }
      : {};

  const [row] = await db
    .update(jobMatches)
    .set({
      deepScore: evaluation.score,
      deepEval: evaluation,
      ...archive,
      updatedAt: new Date(),
    })
    .where(and(eq(jobMatches.userId, userId), eq(jobMatches.id, matchId)))
    .returning();
  return row ?? null;
}

/**
 * Appends to a match's notes rather than replacing them — a note from chat
 * must not erase what the user typed on the job page.
 */
export async function appendMatchNote(
  userId: string,
  matchId: string,
  note: string,
): Promise<JobMatch | null> {
  const text = note.trim();
  if (!text) return null;

  const [row] = await db
    .update(jobMatches)
    .set({
      notes: sql`case when coalesce(${jobMatches.notes}, '') = '' then ${text} else ${jobMatches.notes} || E'\n\n' || ${text} end`,
      updatedAt: new Date(),
    })
    .where(and(eq(jobMatches.userId, userId), eq(jobMatches.id, matchId)))
    .returning();
  return row ?? null;
}

const UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
