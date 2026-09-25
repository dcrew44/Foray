import { eq } from "drizzle-orm";

import { FOLLOW_UP_KINDS } from "@/lib/chat/follow-up-kinds";
import { db } from "@/lib/db";
import { agentEvents, runs } from "@/lib/db/schema";
import type { AgentEvent, NewAgentEvent } from "@/lib/db/schema";
import { QUEUES } from "@/lib/queue/queues";
import { send } from "@/lib/queue/send";

/**
 * Progress is an append-only table, not a socket.
 *
 * Long pipelines write here as they go and the UI polls `/api/live?since=`.
 * That means progress survives a page reload, a worker restart and a browser
 * that was closed for an hour — none of which a stream would.
 */

export type EmitInput = {
  userId: string;
  kind: string;
  message: string;
  runId?: string | null;
  level?: NewAgentEvent["level"];
  data?: unknown;
  approvalId?: string | null;
};

export async function emit(input: EmitInput): Promise<AgentEvent> {
  const [event] = await db
    .insert(agentEvents)
    .values({
      userId: input.userId,
      runId: input.runId ?? null,
      level: input.level ?? "info",
      kind: input.kind,
      message: input.message,
      data: input.data ?? null,
      approvalId: input.approvalId ?? null,
    })
    .returning();

  // A run started from chat reports back there when it pauses, finishes or
  // fails. Hooked here rather than at each call site, so a new kind of run
  // follows up without anyone remembering to wire it.
  if (event.runId && FOLLOW_UP_KINDS.has(event.kind)) {
    await queueFollowUp(event);
  }

  return event;
}

/**
 * Asks the chat agent to report this moment in the conversation that started
 * the run. Runs started from a button have no conversation and are left to
 * the toasts. A failure here must never fail the run that emitted the event.
 */
async function queueFollowUp(event: AgentEvent) {
  try {
    const [run] = await db
      .select({ conversationId: runs.conversationId })
      .from(runs)
      .where(eq(runs.id, event.runId!))
      .limit(1);
    if (!run?.conversationId) return;

    await send(
      QUEUES.chatFollowUp,
      { userId: event.userId, runId: event.runId!, eventId: event.id },
      { singletonKey: `follow-up-${event.id}` },
    );
  } catch (error) {
    console.error(`[events] could not queue a chat follow-up for event ${event.id}:`, error);
  }
}
