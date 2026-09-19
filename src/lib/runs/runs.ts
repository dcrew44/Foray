import { and, desc, eq, sql } from "drizzle-orm";

import { db } from "@/lib/db";
import { runs, type NewRun, type Run } from "@/lib/db/schema";

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
  const [run] = await db
    .select()
    .from(runs)
    .where(and(eq(runs.id, id), eq(runs.userId, userId)))
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
 */
export async function requestCancel(userId: string, id: string) {
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
  return run ?? null;
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
