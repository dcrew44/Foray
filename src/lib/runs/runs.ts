import { and, desc, eq, inArray, sql } from "drizzle-orm";

import { db } from "@/lib/db";
import { runs, type NewRun, type Run } from "@/lib/db/schema";

import { expireRunApprovals } from "./approvals";
import { emit } from "./events";

/**
 * The lifecycle of a background run.
 *
 * Every run kind shares this table, so the activity feed, the run card and
 * cancellation work the same for a search as for a tailor. Stats are merged
 * rather than replaced: a run updates its counters from several places as it
 * progresses, and a whole-object write would lose whatever another step just
 * recorded.
 */

export async function createRun(input: NewRun): Promise<Run> {
  const [run] = await db.insert(runs).values(input).returning();
  return run;
}

export async function getRun(userId: string, id: string): Promise<Run | null> {
  // A malformed id is simply not a run, rather than a database error.
  if (!UUID_RE.test(id)) return null;
  const [run] = await db
    .select()
    .from(runs)
    .where(and(eq(runs.id, id), eq(runs.userId, userId)))
    .limit(1);
  return run ?? null;
}

/**
 * The user's search that is still in flight, if any. Two concurrent searches
 * would score the same postings twice and race each other's approvals, so a
 * new one is refused while this returns a run.
 */
export async function activeSearch(userId: string): Promise<Run | null> {
  const [run] = await db
    .select()
    .from(runs)
    .where(
      and(
        eq(runs.userId, userId),
        eq(runs.kind, "search"),
        inArray(runs.status, ["queued", "running", "awaiting_approval"]),
      ),
    )
    .orderBy(desc(runs.createdAt))
    .limit(1);
  return run ?? null;
}

export async function listRuns(userId: string, limit = 20): Promise<Run[]> {
  return db
    .select()
    .from(runs)
    .where(eq(runs.userId, userId))
    .orderBy(desc(runs.createdAt))
    .limit(limit);
}

export async function startRun(id: string) {
  await db
    .update(runs)
    .set({ status: "running", startedAt: new Date(), updatedAt: new Date() })
    .where(eq(runs.id, id));
}

/** Shallow-merges into `stats`, so concurrent steps do not clobber each other. */
export async function mergeStats(id: string, patch: Record<string, unknown>) {
  await db
    .update(runs)
    .set({
      stats: sql`${runs.stats} || ${JSON.stringify(patch)}::jsonb`,
      updatedAt: new Date(),
    })
    .where(eq(runs.id, id));
}

export async function finishRun(
  id: string,
  outcome:
    | { status: "completed"; output?: unknown }
    | { status: "failed"; error: string }
    | { status: "cancelled" },
) {
  await db
    .update(runs)
    .set({
      status: outcome.status,
      output: "output" in outcome ? (outcome.output ?? null) : null,
      error: "error" in outcome ? outcome.error : null,
      finishedAt: new Date(),
      updatedAt: new Date(),
    })
    .where(eq(runs.id, id));
}

/**
 * Asks a run to stop.
 *
 * Cancellation is cooperative: the handler checks between batches rather than
 * being killed, so a run stops at a consistent point instead of halfway
 * through writing matches. A run that has already finished is left alone.
 *
 * A run paused at an approval has no handler alive to notice, so its open
 * approvals are expired here — the card stops being answerable — and the
 * cancellation is reported here rather than by the handler.
 */
export async function requestCancel(userId: string, id: string) {
  if (!UUID_RE.test(id)) return null;
  const [run] = await db
    .update(runs)
    .set({ status: "cancelled", finishedAt: new Date(), updatedAt: new Date() })
    .where(
      and(
        eq(runs.id, id),
        eq(runs.userId, userId),
        sql`${runs.status} in ('queued', 'running', 'awaiting_approval')`,
      ),
    )
    .returning();
  if (!run) return null;

  const expired = await expireRunApprovals(run.id);
  if (expired > 0) {
    await emit({
      userId,
      runId: run.id,
      kind: "run.cancelled",
      level: "warn",
      message:
        "Cancelled while waiting for your answer. Everything found so far was kept.",
    });
  }

  return run;
}

type RunStatus = Run["status"];

const ACTIVE: RunStatus[] = ["queued", "running", "awaiting_approval"];

/**
 * Moves a run to `to` only if it is still in one of `from`, merging `stats` in
 * the same write. Returns whether it moved — false means something else (a
 * cancel, usually) got there first and the caller should stop.
 */
export async function setRunStatusIf(
  id: string,
  from: RunStatus[],
  to: RunStatus,
  stats?: Record<string, unknown>,
): Promise<boolean> {
  const [row] = await db
    .update(runs)
    .set({
      status: to,
      ...(to === "running"
        ? { startedAt: sql`coalesce(${runs.startedAt}, now())` }
        : {}),
      ...(stats
        ? { stats: sql`${runs.stats} || ${JSON.stringify(stats)}::jsonb` }
        : {}),
      updatedAt: new Date(),
    })
    .where(and(eq(runs.id, id), inArray(runs.status, from)))
    .returning({ id: runs.id });
  return Boolean(row);
}

/**
 * `finishRun` for a run that may have been cancelled under it: finishes only a
 * run that is still active, so a late "completed" never overwrites the user's
 * cancel. Returns whether it finished the run.
 */
export async function finishActiveRun(
  id: string,
  outcome:
    | { status: "completed"; output?: unknown }
    | { status: "failed"; error: string },
  stats?: Record<string, unknown>,
): Promise<boolean> {
  const [row] = await db
    .update(runs)
    .set({
      status: outcome.status,
      output: "output" in outcome ? (outcome.output ?? null) : null,
      error: "error" in outcome ? outcome.error : null,
      ...(stats
        ? { stats: sql`${runs.stats} || ${JSON.stringify(stats)}::jsonb` }
        : {}),
      finishedAt: new Date(),
      updatedAt: new Date(),
    })
    .where(and(eq(runs.id, id), inArray(runs.status, ACTIVE)))
    .returning({ id: runs.id });
  return Boolean(row);
}

/** Checked between batches by long handlers. */
export async function isCancelled(id: string): Promise<boolean> {
  const [run] = await db
    .select({ status: runs.status })
    .from(runs)
    .where(eq(runs.id, id))
    .limit(1);
  return run?.status === "cancelled";
}

/**
 * Fails runs a dead worker left behind.
 *
 * A worker that crashes, is redeployed, or restarts on a file save abandons
 * whatever it was running, and the run row would otherwise say "running"
 * forever — the most confusing state the UI can show. Called once when a
 * worker boots: Foray runs a single worker, so anything still marked running
 * at that moment belongs to one that is gone. Queued runs are left alone
 * (their jobs are still in the queue) and so are runs paused for approval
 * (nothing is in flight for those).
 */
export async function reapInterruptedRuns(): Promise<number> {
  const reaped = await db
    .update(runs)
    .set({
      status: "failed",
      error: "Interrupted: the worker stopped while this was running. Start it again to retry.",
      finishedAt: new Date(),
      updatedAt: new Date(),
    })
    .where(eq(runs.status, "running"))
    .returning({ id: runs.id, userId: runs.userId, kind: runs.kind });

  for (const run of reaped) {
    await emit({
      userId: run.userId,
      runId: run.id,
      kind: "run.failed",
      level: "error",
      message:
        "This run was interrupted because the worker stopped. Everything it finished was kept; start it again to pick up the rest.",
    });
  }

  return reaped.length;
}

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
