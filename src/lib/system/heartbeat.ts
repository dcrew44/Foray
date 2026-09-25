import { eq, sql } from "drizzle-orm";

import { db } from "@/lib/db";
import { systemState } from "@/lib/db/schema";

/**
 * Whether the worker is alive.
 *
 * A search enqueued while the worker is down sits in `queued` forever, and
 * from the user's side that is indistinguishable from a slow search. The
 * worker writes a heartbeat every few seconds; the app reads it and says so
 * plainly when it has stopped.
 */

const KEY = "worker";

/** A heartbeat older than this means the worker is not running. */
export const WORKER_STALE_MS = 45_000;

export type WorkerBeat = {
  pid: number;
  startedAt: string;
  concurrency: number;
};

export async function beat(value: WorkerBeat) {
  await db
    .insert(systemState)
    .values({ key: KEY, value, updatedAt: new Date() })
    .onConflictDoUpdate({
      target: systemState.key,
      set: { value, updatedAt: sql`now()` },
    });
}

export type WorkerStatus = {
  online: boolean;
  lastSeenAt: string | null;
  pid: number | null;
};

export async function workerStatus(): Promise<WorkerStatus> {
  const [row] = await db
    .select()
    .from(systemState)
    .where(eq(systemState.key, KEY))
    .limit(1);

  if (!row) return { online: false, lastSeenAt: null, pid: null };

  const value = row.value as Partial<WorkerBeat>;
  return {
    online: Date.now() - row.updatedAt.getTime() < WORKER_STALE_MS,
    lastSeenAt: row.updatedAt.toISOString(),
    pid: value.pid ?? null,
  };
}
