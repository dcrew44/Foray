import { and, asc, desc, eq, sql } from "drizzle-orm";
import type { UIMessage } from "ai";

import { db } from "@/lib/db";
import { conversations, messages } from "@/lib/db/schema";

/**
 * Conversation and message reads/writes.
 *
 * Every function takes `userId` first and filters on it. There is no read path
 * to another user's conversation — DESIGN.md invariant 8.
 */

export async function listConversations(userId: string, limit = 50) {
  return db
    .select()
    .from(conversations)
    .where(eq(conversations.userId, userId))
    .orderBy(desc(conversations.updatedAt))
    .limit(limit);
}

/** The conversation, or null if it does not exist *or* belongs to someone else. */
export async function getConversation(userId: string, id: string) {
  const [row] = await db
    .select()
    .from(conversations)
    .where(and(eq(conversations.id, id), eq(conversations.userId, userId)))
    .limit(1);
  return row ?? null;
}

export async function createConversation(userId: string, title?: string) {
  const [row] = await db
    .insert(conversations)
    .values({ userId, title: title ?? null })
    .returning();
  return row;
}

/**
 * Resolves the conversation for a chat turn, creating it on the first message.
 *
 * The client picks the id before it sends anything, so that the URL it will
 * navigate to afterwards is known up front. The server therefore *claims* that
 * id rather than minting its own — otherwise the client would redirect to a
 * conversation that does not exist.
 *
 * Claiming is safe because it cannot take an id that is already held: the
 * insert does nothing on conflict, and an id belonging to someone else falls
 * through to a fresh conversation rather than attaching to theirs.
 */
export async function ensureConversation(
  userId: string,
  id: string | undefined,
  title?: string,
) {
  if (id && UUID.test(id)) {
    const existing = await getConversation(userId, id);
    if (existing) return existing;

    const [claimed] = await db
      .insert(conversations)
      .values({ id, userId, title: title ?? null })
      .onConflictDoNothing()
      .returning();
    if (claimed) return claimed;
  }
  return createConversation(userId, title);
}

const UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export async function loadMessages(
  userId: string,
  conversationId: string,
): Promise<UIMessage[]> {
  const rows = await db
    .select()
    .from(messages)
    .where(
      and(
        eq(messages.conversationId, conversationId),
        eq(messages.userId, userId),
      ),
    )
    .orderBy(asc(messages.createdAt));

  return rows.map((row) => ({
    id: row.id,
    role: row.role as UIMessage["role"],
    parts: row.parts as UIMessage["parts"],
    metadata: row.metadata ?? undefined,
  }));
}

/**
 * Upserts by the AI SDK's own message id.
 *
 * A streamed assistant message is written once when the stream ends, but an
 * agent that takes several steps can persist the same message id more than
 * once — the later write must update, not duplicate.
 */
export async function saveMessages(
  userId: string,
  conversationId: string,
  uiMessages: UIMessage[],
) {
  if (uiMessages.length === 0) return;

  await db
    .insert(messages)
    .values(
      uiMessages.map((message) => ({
        id: message.id,
        conversationId,
        userId,
        role: message.role,
        parts: message.parts,
        metadata: message.metadata ?? null,
      })),
    )
    .onConflictDoUpdate({
      target: messages.id,
      // `excluded` is the row Postgres was about to insert.
      set: {
        parts: sql`excluded.parts`,
        metadata: sql`excluded.metadata`,
      },
    });

  await db
    .update(conversations)
    .set({ updatedAt: new Date() })
    .where(eq(conversations.id, conversationId));
}
