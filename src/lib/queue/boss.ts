import { PgBoss } from "pg-boss";

import { env } from "@/env";
import { QUEUES, type QueueName } from "./queues";

/**
 * pg-boss configuration shared by both entry points.
 *
 * The queue lives in the same Postgres as everything else, in its own `pgboss`
 * schema — no Redis, and a job and the rows it writes can share a transaction.
 */
export function createBoss(options: {
  /** Worker processes supervise and migrate; the app only sends. */
  role: "worker" | "sender";
}) {
  return new PgBoss({
    connectionString: env.DATABASE_URL,
    schema: "pgboss",
    // The worker owns the pgboss schema. If the app migrated it too, two
    // processes could race the same DDL on a cold start.
    migrate: options.role === "worker",
    supervise: options.role === "worker",
    schedule: options.role === "worker",
  });
}

/**
 * Queues must exist before anything is sent to them, so both the worker and
 * the sender declare the full set. Creating one that already exists is a no-op.
 */
export async function ensureQueues(boss: PgBoss) {
  for (const name of Object.values(QUEUES) as QueueName[]) {
    await boss.createQueue(name);
  }
}
