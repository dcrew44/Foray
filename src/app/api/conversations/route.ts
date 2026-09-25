import { NextResponse } from "next/server";

import { requireUser } from "@/lib/auth/session";
import { listConversations } from "@/lib/db/queries/conversations";

/** The sidebar's recent chats, newest activity first. */
export async function GET() {
  const user = await requireUser();
  const rows = await listConversations(user.id, 30);

  return NextResponse.json(
    {
      conversations: rows.map((row) => ({
        id: row.id,
        title: row.title ?? "Untitled chat",
        updatedAt: row.updatedAt.toISOString(),
      })),
    },
    { headers: { "Cache-Control": "no-store" } },
  );
}
