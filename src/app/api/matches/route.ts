import { NextResponse } from "next/server";

import { requireUser } from "@/lib/auth/session";
import { getMatchRows } from "@/lib/db/queries/matches";

/**
 * Match rows by id, for cards that know which matches they want to show — a
 * finished run card listing its best results. `?ids=a,b,c`, at most 20.
 */
export async function GET(request: Request) {
  const user = await requireUser();
  const ids = (new URL(request.url).searchParams.get("ids") ?? "")
    .split(",")
    .map((id) => id.trim())
    .filter((id) => /^[0-9a-f-]{36}$/i.test(id))
    .slice(0, 20);

  const rows = await getMatchRows(user.id, ids);
  return NextResponse.json({ matches: rows }, { headers: { "Cache-Control": "no-store" } });
}
