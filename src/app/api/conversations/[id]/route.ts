import { NextResponse } from "next/server";

import { requireUser } from "@/lib/auth/session";
import { deleteConversation } from "@/lib/db/queries/conversations";

/** Deletes one of the signed-in user's conversations and its messages. */
export async function DELETE(
  _request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await requireUser();
  const deleted = await deleteConversation(user.id, (await params).id);
  if (!deleted) return NextResponse.json({ error: "Not found" }, { status: 404 });
  return NextResponse.json({ ok: true });
}
