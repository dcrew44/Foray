import type { Job } from "pg-boss";

import { emit } from "@/lib/runs/events";
import type { PingPayload } from "@/lib/queue/queues";

/**
 * The liveness check: the app enqueued something, this process picked it up,
 * and the event it writes shows up in the activity feed.
 *
 * pg-boss 12 hands a handler an array of jobs, even when batchSize is 1.
 */
export async function handlePing(jobs: Job<PingPayload>[]) {
  for (const job of jobs) {
    await emit({
      userId: job.data.userId,
      kind: "worker.ping",
      message: job.data.note ?? "Worker is alive.",
      data: { jobId: job.id, workerPid: process.pid },
    });
    console.log(`[worker] ping ${job.id} for user ${job.data.userId}`);
  }
}
