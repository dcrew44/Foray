import { NextResponse } from "next/server";

import { requireUser } from "@/lib/auth/session";
import { expireStaleApprovals } from "@/lib/runs/approvals";
import { liveSnapshot } from "@/lib/runs/live";

/**
 * The app shell's single poll: new events since the cursor, active and
 * recently finished runs, pending approvals, and whether the worker is alive.
 * Scoped to the signed-in user.
 */
export async function GET(request: Request) {
  const user = await requireUser();

  const raw = Number(new URL(request.url).searchParams.get("since") ?? 0);
  const since = Number.isFinite(raw) && raw > 0 ? raw : 0;

  // On the first poll of a page load, let lapsed approvals resume their runs
  // (as a "no"), so a run paused a week ago does not sit waiting forever with
  // no card to answer. Once per load, not on every tick.
  if (since === 0) await expireStaleApprovals(user.id);

  return NextResponse.json(await liveSnapshot(user.id, since), {
    headers: { "Cache-Control": "no-store" },
  });
}
