import { NextResponse } from "next/server";

import { requireUser } from "@/lib/auth/session";
import {
  deleteConversation,
  getConversation,
  loadMessages,
} from "@/lib/db/queries/conversations";

type Params = { params: Promise<{ id: string }> };

/**
 * One of the signed-in user's conversations, with its messages. The chat
 * reads it when the agent has followed up on a run in the background.
 */
export async function GET(_request: Request, { params }: Params) {
  const user = await requireUser();
  const conversation = await getConversation(user.id, (await params).id);
  if (!conversation) return NextResponse.json({ error: "Not found" }, { status: 404 });
  const messages = await loadMessages(user.id, conversation.id);
  return NextResponse.json({ messages }, { headers: { "Cache-Control": "no-store" } });
}

/** Deletes one of the signed-in user's conversations and its messages. */
export async function DELETE(_request: Request, { params }: Params) {
  const user = await requireUser();
  const deleted = await deleteConversation(user.id, (await params).id);
  if (!deleted) return NextResponse.json({ error: "Not found" }, { status: 404 });
  return NextResponse.json({ ok: true });
}
