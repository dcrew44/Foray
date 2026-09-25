import { NextResponse } from "next/server";

import { requireUser } from "@/lib/auth/session";
import { toLiveRun } from "@/lib/runs/live";
import { getRun } from "@/lib/runs/runs";

/** One run, in the same shape the live snapshot uses. */
export async function GET(
  _request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await requireUser();
  const run = await getRun(user.id, (await params).id);

  // Someone else's run is indistinguishable from one that does not exist.
  if (!run) return NextResponse.json({ error: "Not found" }, { status: 404 });

  return NextResponse.json(toLiveRun(run), {
    headers: { "Cache-Control": "no-store" },
  });
}
