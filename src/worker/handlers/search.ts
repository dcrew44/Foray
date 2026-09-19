import type { Job } from "pg-boss";

import { getRun } from "@/lib/runs/runs";
import { executeSearchRun } from "@/lib/runs/search-run";
import type { SearchPayload } from "@/lib/queue/queues";

/**
 * A search run, off the request path.
 *
 * Refreshing boards and filtering the mirror takes tens of seconds, which is
 * far past what a chat turn can wait for. The chat tool enqueues this and
 * returns a run id immediately; progress reaches the user through agent_events.
 */
export async function handleSearch(jobs: Job<SearchPayload>[]) {
  for (const job of jobs) {
    const run = await getRun(job.data.userId, job.data.runId);
    if (!run) {
      console.error(`[worker] search: no run ${job.data.runId}`);
      continue;
    }

    // A run cancelled before the worker picked it up should not start.
    if (run.status === "cancelled") continue;

    console.log(`[worker] search run ${run.id}`);
    await executeSearchRun(run);
  }
}
