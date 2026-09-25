import "@/lib/load-dotenv";

import type { Job, PgBoss } from "pg-boss";

import { env } from "@/env";
import { pool } from "@/lib/db";
import { createBoss, ensureQueues } from "@/lib/queue/boss";
import { QUEUES, type QueueName, type QueuePayloads } from "@/lib/queue/queues";
import { reapInterruptedRuns } from "@/lib/runs/runs";
import { beat } from "@/lib/system/heartbeat";

import { handleChatFollowUp } from "./handlers/chat";
import {
  handleCompanyDiscover,
  handleCompanyRefresh,
} from "./handlers/company";
import {
  handleMatchEvaluate,
  handleSearchEvaluate,
  handleSearchShortlist,
} from "./handlers/evaluate";
import { handlePing } from "./handlers/ping";
import { handleResumeRender, handleResumeTailor } from "./handlers/resume";
import { handleSearch } from "./handlers/search";

/**
 * The long-running half of Foray.
 *
 * Everything slow lives here — source fetching, scoring, evaluation,
 * tailoring, PDF rendering, and in Phase 2 the browser agent — so a Next
 * request never waits on it. This process must never import from src/app;
 * ESLint enforces that.
 */

const boss = createBoss({ role: "worker" });

boss.on("error", (error) => console.error("[worker] pg-boss error:", error));

type Handler<K extends QueueName> = (jobs: Job<QueuePayloads[K]>[]) => Promise<void>;

/**
 * Every queue this process works, and how many of its jobs may run at once.
 *
 * Concurrency is per queue, so a long search never blocks a tailoring job
 * the user is waiting on. The LLM work inside each is still bounded by the
 * per-provider limiter, which is where a provider's real rate limit lives.
 */
const heavy = Math.max(1, Math.min(env.WORKER_CONCURRENCY, 4));

const REGISTRATIONS: {
  [K in QueueName]: { handler: Handler<K>; concurrency: number };
} = {
  [QUEUES.ping]: { handler: handlePing, concurrency: 1 },
  [QUEUES.companyDiscover]: { handler: handleCompanyDiscover, concurrency: 2 },
  // One at a time: a refresh already fans out across boards internally, and
  // two overlapping refreshes would double the load on free public APIs.
  [QUEUES.companyRefresh]: { handler: handleCompanyRefresh, concurrency: 1 },
  [QUEUES.search]: { handler: handleSearch, concurrency: heavy },
  [QUEUES.searchEvaluate]: { handler: handleSearchEvaluate, concurrency: heavy },
  [QUEUES.searchShortlist]: { handler: handleSearchShortlist, concurrency: heavy },
  [QUEUES.matchEvaluate]: { handler: handleMatchEvaluate, concurrency: heavy },
  [QUEUES.resumeTailor]: { handler: handleResumeTailor, concurrency: heavy },
  [QUEUES.resumeRender]: { handler: handleResumeRender, concurrency: 2 },
  [QUEUES.chatFollowUp]: { handler: handleChatFollowUp, concurrency: 2 },
};

async function register(worker: PgBoss) {
  for (const [queue, registration] of Object.entries(REGISTRATIONS)) {
    const { handler, concurrency } = registration as {
      handler: (jobs: Job<unknown>[]) => Promise<void>;
      concurrency: number;
    };
    await worker.work(
      queue,
      { batchSize: 1, localConcurrency: concurrency },
      async (jobs: Job<unknown>[]) => {
        try {
          await handler(jobs);
        } catch (error) {
          // Handlers record failures on their own run; this is the backstop
          // for one that throws before it has a run to record on.
          console.error(`[worker] ${queue} failed:`, error);
          throw error;
        }
      },
    );
  }
}

const startedAt = new Date().toISOString();
let heartbeat: NodeJS.Timeout | undefined;
/**
 * Set once a signal arrives. A signal can land while main() is still booting
 * — a watch restart moments after the last one — and booting on after the
 * pool has been closed only produces a crash that hides the real log.
 */
let stopping = false;

async function main() {
  await boss.start();
  await ensureQueues(boss);
  if (stopping) return;

  // Before taking new work: anything still "running" was abandoned by the
  // previous worker process and would otherwise look busy forever.
  const reaped = await reapInterruptedRuns();
  if (reaped > 0) console.log(`[worker] marked ${reaped} interrupted run(s) failed`);
  if (stopping) return;

  await register(boss);

  /**
   * Keep the mirror warm. Every six hours, any board not fetched in the last
   * two hours is refetched, and postings that have vanished are marked
   * inactive. `missed: "once"` means a laptop that was closed overnight
   * catches up with a single run rather than firing every window it slept
   * through.
   */
  await boss.schedule(QUEUES.companyRefresh, "0 */6 * * *", {}, {
    key: "company-refresh-cron",
    missed: "once",
  });

  // The app shows "worker offline" when this goes quiet, so a queued search
  // is never mistaken for a slow one.
  const pulse = () =>
    beat({ pid: process.pid, startedAt, concurrency: heavy }).catch((error) =>
      console.error("[worker] heartbeat failed:", error),
    );
  await pulse();
  heartbeat = setInterval(pulse, 10_000);

  console.log(`[worker] ready (pid ${process.pid}, concurrency ${heavy})`);
}

/**
 * Stop fetching new jobs, let in-flight ones finish, then close the pool. A
 * hard exit would leave jobs marked active until pg-boss's expiry reaped them.
 */
async function shutdown(signal: string) {
  if (stopping) return;
  stopping = true;
  console.log(`[worker] ${signal} — draining`);
  clearInterval(heartbeat);
  try {
    await boss.stop({ graceful: true, close: true, timeout: 30_000 });
    await pool.end();
  } catch (error) {
    console.error("[worker] shutdown error:", error);
  } finally {
    process.exit(0);
  }
}

for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.on(signal, () => void shutdown(signal));
}

main().catch((error) => {
  if (stopping) return;
  console.error("[worker] failed to start:", error);
  process.exit(1);
});
