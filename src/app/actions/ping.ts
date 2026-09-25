"use server";

import { requireUser } from "@/lib/auth/session";
import { QUEUES } from "@/lib/queue/queues";
import { send } from "@/lib/queue/send";

/** Enqueues the liveness check and returns immediately — Next never waits. */
export async function pingWorker(): Promise<{ jobId: string | null }> {
  const user = await requireUser();
  const jobId = await send(QUEUES.ping, {
    userId: user.id,
    note: "The worker answered a test job from Settings.",
  });
  return { jobId };
}
