import type { Job } from "pg-boss";

import { runChatFollowUp } from "@/lib/chat/follow-up";
import type { ChatFollowUpPayload } from "@/lib/queue/queues";

/** The agent reporting back in the chat that started a run — see lib/chat/follow-up.ts. */
export async function handleChatFollowUp(jobs: Job<ChatFollowUpPayload>[]) {
  for (const job of jobs) {
    const outcome = await runChatFollowUp(job.data);
    console.log(`[worker] chat follow-up for run ${job.data.runId}: ${outcome}`);
  }
}
