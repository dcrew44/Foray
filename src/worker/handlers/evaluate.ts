import type { Job } from "pg-boss";

import type {
  MatchEvaluatePayload,
  SearchContinuationPayload,
} from "@/lib/queue/queues";
import {
  continueSearchEvaluate,
  continueSearchShortlist,
  executeMatchEvaluate,
  failRun,
} from "@/lib/runs/evaluate-run";

/**
 * Deep evaluation and shortlisting — the continuations of a search run's two
 * approval gates, plus on-demand evaluation of specific matches.
 *
 * The work lives in lib/runs/evaluate-run.ts, which records every outcome on
 * the run itself. What is here is the backstop for anything that escapes it —
 * a database error before the run was even loaded — so a run is never left
 * looking busy after its handler has died.
 */

export async function handleSearchEvaluate(
  jobs: Job<SearchContinuationPayload>[],
) {
  for (const job of jobs) {
    console.log(`[worker] search.evaluate run ${job.data.runId}`);
    await backstop(job.data, () => continueSearchEvaluate(job.data));
  }
}

export async function handleSearchShortlist(
  jobs: Job<SearchContinuationPayload>[],
) {
  for (const job of jobs) {
    console.log(`[worker] search.shortlist run ${job.data.runId}`);
    await backstop(job.data, () => continueSearchShortlist(job.data));
  }
}

export async function handleMatchEvaluate(jobs: Job<MatchEvaluatePayload>[]) {
  for (const job of jobs) {
    console.log(`[worker] match.evaluate run ${job.data.runId}`);
    await backstop(job.data, () => executeMatchEvaluate(job.data));
  }
}

async function backstop(
  payload: { userId: string; runId: string },
  work: () => Promise<void>,
) {
  try {
    await work();
  } catch (error) {
    try {
      await failRun({ id: payload.runId, userId: payload.userId }, error);
    } catch (inner) {
      console.error(`[worker] could not record failure of run ${payload.runId}:`, inner);
    }
  }
}
