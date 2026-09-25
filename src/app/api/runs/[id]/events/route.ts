import { and, asc, eq } from "drizzle-orm";
import { NextResponse } from "next/server";

import { requireUser } from "@/lib/auth/session";
import { db } from "@/lib/db";
import { agentEvents } from "@/lib/db/schema";
import type { LiveEvent } from "@/lib/runs/live";
import { getRun } from "@/lib/runs/runs";

/** A long run writes a few hundred events at most; this is a backstop, not a page size. */
const LIMIT = 1000;

/**
 * Every event one run wrote, oldest first, in the live feed's shape — for the
 * activity page, where a run from last week is expanded long after its events
 * left the live window.
 */
export async function GET(
  _request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  const user = await requireUser();
  const run = await getRun(user.id, (await params).id);

  // Someone else's run is indistinguishable from one that does not exist.
  if (!run) return NextResponse.json({ error: "Not found" }, { status: 404 });

  const rows = await db
    .select()
    .from(agentEvents)
    .where(and(eq(agentEvents.userId, user.id), eq(agentEvents.runId, run.id)))
    .orderBy(asc(agentEvents.id))
    .limit(LIMIT);

  const events: LiveEvent[] = rows.map((event) => ({
    id: event.id,
    runId: event.runId,
    level: event.level,
    kind: event.kind,
    message: event.message,
    data: event.data,
    approvalId: event.approvalId,
    createdAt: event.createdAt.toISOString(),
  }));

  return NextResponse.json({ events }, { headers: { "Cache-Control": "no-store" } });
}
