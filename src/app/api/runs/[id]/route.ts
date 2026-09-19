import { NextResponse } from "next/server";

import { requireUser } from "@/lib/auth/session";
import { getRun } from "@/lib/runs/runs";

/** What the run card polls while a run is in flight. */
export async function GET(
  _request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await requireUser();
  const run = await getRun(user.id, (await params).id);

  // Someone else's run is indistinguishable from one that does not exist.
  if (!run) return NextResponse.json({ error: "Not found" }, { status: 404 });

  return NextResponse.json({
    id: run.id,
    kind: run.kind,
    status: run.status,
    mode: run.mode,
    stats: run.stats,
    output: run.output,
    error: run.error,
    startedAt: run.startedAt,
    finishedAt: run.finishedAt,
  });
}
