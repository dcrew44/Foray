import { NextResponse } from "next/server";

import { requireUser } from "@/lib/auth/session";
import { eventsSince } from "@/lib/runs/events";

/**
 * The activity feed's poll endpoint.
 *
 * `since` is the last event id the client holds; the response carries the new
 * events and the id to ask with next time. Scoped to the signed-in user — there
 * is no way to ask for anyone else's events.
 */
export async function GET(request: Request) {
  const user = await requireUser();

  const since = Number(new URL(request.url).searchParams.get("since") ?? 0);
  const events = await eventsSince(user.id, Number.isFinite(since) ? since : 0);

  return NextResponse.json({
    events,
    cursor: events.at(-1)?.id ?? (Number.isFinite(since) ? since : 0),
  });
}
