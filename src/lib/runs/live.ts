import { and, desc, eq, gt, inArray, isNull, or, sql } from "drizzle-orm";

import { db } from "@/lib/db";
import { agentEvents, approvals, runs } from "@/lib/db/schema";
import { describeBrief, parseBrief } from "@/lib/ranking/brief";
import { workerStatus, type WorkerStatus } from "@/lib/system/heartbeat";

/**
 * Everything the UI needs to stay current, in one read.
 *
 * The app used to poll per component — the activity feed its endpoint, every
 * run card its own — so a page with three cards made four requests every two
 * seconds and each showed a slightly different moment. One snapshot, fetched
 * by one provider in the app shell, keeps the sidebar badge, the run cards,
 * the approval cards and the toasts agreeing with each other.
 */

export type LiveEvent = {
  id: number;
  runId: string | null;
  level: "debug" | "info" | "warn" | "error";
  kind: string;
  message: string;
  data: unknown;
  approvalId: string | null;
  createdAt: string;
};

export type LiveRun = {
  id: string;
  kind: string;
  status:
    | "queued"
    | "running"
    | "awaiting_approval"
    | "completed"
    | "failed"
    | "cancelled";
  mode: "approve_each" | "autopilot";
  prompt: string | null;
  /** For a search: what it is for, in one line, once the brief is known. */
  summary: string | null;
  stats: Record<string, unknown>;
  output: Record<string, unknown> | null;
  error: string | null;
  createdAt: string;
  startedAt: string | null;
  finishedAt: string | null;
};

export type LiveApproval = {
  id: string;
  runId: string;
  kind: "deep_eval_batch" | "shortlist_proposal" | "tailor" | "company_add";
  title: string;
  payload: Record<string, unknown>;
  createdAt: string;
};

export type LiveSnapshot = {
  cursor: number;
  events: LiveEvent[];
  runs: LiveRun[];
  approvals: LiveApproval[];
  worker: WorkerStatus;
};

/** Finished runs stay in the snapshot this long, so cards see the final state. */
const RECENT_MS = 10 * 60 * 1000;

export function toLiveRun(run: typeof runs.$inferSelect): LiveRun {
  const input = (run.input ?? {}) as {
    prompt?: string | null;
    brief?: unknown;
    hints?: unknown;
  };
  return {
    id: run.id,
    kind: run.kind,
    status: run.status,
    mode: run.mode,
    prompt: input.prompt ?? null,
    summary:
      run.kind === "search"
        ? describeBrief(parseBrief(input.brief ?? input.hints))
        : null,
    stats: (run.stats ?? {}) as Record<string, unknown>,
    output: (run.output ?? null) as Record<string, unknown> | null,
    error: run.error,
    createdAt: run.createdAt.toISOString(),
    startedAt: run.startedAt?.toISOString() ?? null,
    finishedAt: run.finishedAt?.toISOString() ?? null,
  };
}

export async function liveSnapshot(
  userId: string,
  since: number,
): Promise<LiveSnapshot> {
  const recent = new Date(Date.now() - RECENT_MS);

  const [events, runRows, approvalRows, worker] = await Promise.all([
    since > 0
      ? db
          .select()
          .from(agentEvents)
          .where(and(eq(agentEvents.userId, userId), gt(agentEvents.id, since)))
          .orderBy(agentEvents.id)
          .limit(200)
      : // First load: the latest slice, not the whole history.
        db
          .select()
          .from(agentEvents)
          .where(eq(agentEvents.userId, userId))
          .orderBy(desc(agentEvents.id))
          .limit(60)
          .then((rows) => rows.reverse()),
    db
      .select()
      .from(runs)
      .where(
        and(
          eq(runs.userId, userId),
          or(
            inArray(runs.status, ["queued", "running", "awaiting_approval"]),
            gt(runs.updatedAt, recent),
          ),
        ),
      )
      .orderBy(desc(runs.createdAt))
      .limit(20),
    db
      .select()
      .from(approvals)
      .where(
        and(
          eq(approvals.userId, userId),
          eq(approvals.status, "pending"),
          or(isNull(approvals.expiresAt), sql`${approvals.expiresAt} > now()`),
        ),
      )
      .orderBy(desc(approvals.createdAt))
      .limit(20),
    workerStatus(),
  ]);

  const liveEvents: LiveEvent[] = events.map((event) => ({
    id: event.id,
    runId: event.runId,
    level: event.level,
    kind: event.kind,
    message: event.message,
    data: event.data,
    approvalId: event.approvalId,
    createdAt: event.createdAt.toISOString(),
  }));

  return {
    cursor: liveEvents.at(-1)?.id ?? since,
    events: liveEvents,
    runs: runRows.map(toLiveRun),
    approvals: approvalRows.map((approval) => ({
      id: approval.id,
      runId: approval.runId,
      kind: approval.kind,
      title: approval.title,
      payload: (approval.payload ?? {}) as Record<string, unknown>,
      createdAt: approval.createdAt.toISOString(),
    })),
    worker,
  };
}
