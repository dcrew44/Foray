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
    // Wake the worker the moment a job is sent rather than on its next poll,
    // so "Search started" is followed by activity within a beat, not seconds.
    useListenNotify: options.role === "worker",
  });
}

type QueuePolicy = {
  /**
   * Retries after a thrown error. Zero for anything that spends money on a
   * model: those handlers record their own failure on the run, and a silent
   * automatic re-run would pay for the same work twice. pg-boss defaults to 2.
   */
  retryLimit: number;
  /** How long a job may stay active before pg-boss considers it dead. */
  expireInSeconds: number;
};

const MINUTE = 60;

const POLICIES: Record<QueueName, QueuePolicy> = {
  [QUEUES.ping]: { retryLimit: 0, expireInSeconds: MINUTE },
  [QUEUES.companyDiscover]: { retryLimit: 1, expireInSeconds: 15 * MINUTE },
  [QUEUES.companyRefresh]: { retryLimit: 0, expireInSeconds: 60 * MINUTE },
  [QUEUES.search]: { retryLimit: 0, expireInSeconds: 60 * MINUTE },
  [QUEUES.searchEvaluate]: { retryLimit: 0, expireInSeconds: 60 * MINUTE },
  [QUEUES.searchShortlist]: { retryLimit: 0, expireInSeconds: 5 * MINUTE },
  [QUEUES.matchEvaluate]: { retryLimit: 0, expireInSeconds: 30 * MINUTE },
  [QUEUES.resumeTailor]: { retryLimit: 0, expireInSeconds: 30 * MINUTE },
  [QUEUES.resumeRender]: { retryLimit: 1, expireInSeconds: 10 * MINUTE },
};

/**
 * Queues must exist before anything is sent to them, so both the worker and
 * the sender declare the full set. Creating one that already exists is a
 * no-op, so the policy is applied with an update as well — otherwise a policy
 * change here would never reach a queue created by an earlier version.
 */
export async function ensureQueues(boss: PgBoss) {
  for (const name of Object.values(QUEUES) as QueueName[]) {
    const policy = POLICIES[name];
    await boss.createQueue(name, { ...policy, notify: true });
    await boss.updateQueue(name, { ...policy, notify: true });
  }
}
