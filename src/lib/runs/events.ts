import { and, asc, eq, gt } from "drizzle-orm";

import { db } from "@/lib/db";
import { agentEvents } from "@/lib/db/schema";
import type { AgentEvent, NewAgentEvent } from "@/lib/db/schema";

/**
 * Progress is an append-only table, not a socket.
 *
 * Long pipelines write here as they go and the UI polls `/api/events?since=`.
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

  return event;
}

/**
 * Events for one user newer than `since`. `since` is the last id the client
 * saw, so a client that misses a poll catches up rather than skipping ahead.
 */
export async function eventsSince(
  userId: string,
  since: number,
  limit = 200,
): Promise<AgentEvent[]> {
  return db
    .select()
    .from(agentEvents)
    .where(and(eq(agentEvents.userId, userId), gt(agentEvents.id, since)))
    .orderBy(asc(agentEvents.id))
    .limit(limit);
}
