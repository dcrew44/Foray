import "@/lib/load-dotenv";

import { env } from "@/env";
import { createBoss, ensureQueues } from "@/lib/queue/boss";
import { QUEUES } from "@/lib/queue/queues";
import { pool } from "@/lib/db";

import { handlePing } from "./handlers/ping";

/**
 * The long-running half of Foray.
 *
 * Everything slow lives here — source fetching, scoring, tailoring, and in
 * Phase 2 the browser agent — so a Next request never waits on it. This process
 * must never import from src/app; ESLint enforces that.
 */

const boss = createBoss({ role: "worker" });

boss.on("error", (error) => console.error("[worker] pg-boss error:", error));

async function main() {
  await boss.start();
  await ensureQueues(boss);

  await boss.work<import("@/lib/queue/queues").PingPayload>(
    QUEUES.ping,
    { batchSize: 1 },
    handlePing,
  );

  console.log(
    `[worker] ready (pid ${process.pid}, concurrency ${env.WORKER_CONCURRENCY})`,
  );
}

/**
 * Stop fetching new jobs, let in-flight ones finish, then close the pool. A
 * hard exit would leave jobs marked active until pg-boss's expiry reaped them.
 */
async function shutdown(signal: string) {
  console.log(`[worker] ${signal} — draining`);
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
  console.error("[worker] failed to start:", error);
  process.exit(1);
});
