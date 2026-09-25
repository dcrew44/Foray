import { randomUUID } from "node:crypto";

import { and, desc, eq, gt, inArray, isNull, lte, or, sql } from "drizzle-orm";

import { db } from "@/lib/db";
import { approvals, runs, type Approval } from "@/lib/db/schema";
import { QUEUES, type QueueName, type QueuePayloads } from "@/lib/queue/queues";
import { send } from "@/lib/queue/send";

import { emit } from "./events";

/**
 * The approval gate — the durable-workflow substitute every later phase reuses.
 *
 * A pipeline that reaches a decision point writes an `approvals` row carrying
 * its `continuation` (a queue name and payload), pauses its run and returns;
 * nothing stays alive waiting. Deciding — from the UI or from chat — enqueues
 * that continuation, which picks the run back up. In `autopilot` the same row
 * is written already approved and the continuation is enqueued at once, so
 * there is one code path rather than two.
 *
 * Every decision enqueues the continuation, a "no" included: the continuation
 * is what finishes the run, so a rejection that did not resume it would leave
 * the run paused forever. Continuations must therefore treat any status other
 * than approved or auto_approved as a no — see `isApproved`.
 */

/** A pending approval nobody answers stops being answerable after this. */
export const APPROVAL_TTL_MS = 7 * 24 * 60 * 60 * 1000;

export type ApprovalKind = Approval["kind"];
export type ApprovalMode = "approve_each" | "autopilot";

/** What gets enqueued when an approval is decided. */
export type Continuation = {
  [K in QueueName]: { queue: K; payload: QueuePayloads[K] };
}[QueueName];

/** What a person (or the chat agent on their behalf) answers. */
export type ApprovalDecision = {
  approved: boolean;
  /** A subset of the payload's items. Absent means all of them. */
  itemIds?: string[];
};

/**
 * What `approvals.decision` holds. `itemIds` is present whenever the payload
 * has items: the subset that was approved, in the payload's order, or `[]` for
 * a rejection.
 */
export type StoredDecision = {
  approved: boolean;
  itemIds?: string[];
};

// --- Payloads of the kinds the search pipeline raises ------------------------

export type CostEstimate = {
  /** Total for every item, in USD. Null when the model has no price on file. */
  usd: number | null;
  perItemUsd: number | null;
  /** `provider:model`, e.g. `openai:gpt-5.6-terra`. */
  model: string;
};

export type DeepEvalBatchItem = {
  matchId: string;
  jobId: string;
  title: string;
  company: string;
  fastScore: number | null;
};

/** `deep_eval_batch`: which matches to spend the strong model on, and the cost. */
export type DeepEvalBatchPayload = {
  items: DeepEvalBatchItem[];
  estimate: CostEstimate;
  /** Anything worth saying about the batch, e.g. that it includes older matches. */
  note: string | null;
};

export type ShortlistItem = {
  matchId: string;
  jobId: string;
  title: string;
  company: string;
  deepScore: number | null;
  recommendation: "strong" | "apply" | "maybe" | "skip";
  summary: string;
};

/** `shortlist_proposal`: evaluated matches worth moving to the shortlist. */
export type ShortlistPayload = {
  items: ShortlistItem[];
};

// --- Raising a gate ------------------------------------------------------------

export type RequestApprovalInput = {
  userId: string;
  runId: string;
  kind: ApprovalKind;
  title: string;
  /**
   * What is being asked. Items, when there are any, go in `items` and are
   * identified by `matchId` (or `id` for items that are not matches) — that is
   * what a decision's `itemIds` refer to.
   */
  payload: object;
  /**
   * The continuation, or a function building it from the approval's id — a
   * continuation usually needs to know which approval it is resuming from, and
   * the id is assigned here.
   */
  continuation: Continuation | ((approvalId: string) => Continuation);
  mode: ApprovalMode;
  /**
   * Merged into the run's stats: in the same write that pauses the run, or,
   * on autopilot, before the continuation is enqueued.
   */
  stats?: Record<string, unknown>;
  /** Activity-feed messages. Both default to something built from the title. */
  messages?: { requested?: string; auto?: string };
  /** Extra data for the event. */
  data?: Record<string, unknown>;
};

/**
 * Raises a gate on a run.
 *
 * `approve_each`: writes a pending approval, pauses the run
 * (`awaiting_approval`) and emits `approval.requested` carrying the approval id
 * so the feed can render the card inline. The caller should return afterwards;
 * the decision resumes the run.
 *
 * `autopilot`: writes the approval already `auto_approved` for every item,
 * emits `approval.auto`, and enqueues the continuation immediately.
 *
 * A run cancelled while this was being written gets its approval expired on
 * the spot, so a cancelled run never shows an answerable card.
 */
export async function requestApproval(
  input: RequestApprovalInput,
): Promise<Approval> {
  const id = randomUUID();
  const continuation =
    typeof input.continuation === "function"
      ? input.continuation(id)
      : input.continuation;

  if (input.mode === "autopilot") {
    const [approval] = await db
      .insert(approvals)
      .values({
        id,
        userId: input.userId,
        runId: input.runId,
        kind: input.kind,
        title: input.title,
        payload: input.payload,
        continuation,
        status: "auto_approved",
        decision: (hasItems(input.payload)
          ? { approved: true, itemIds: payloadItemIds(input.payload) }
          : { approved: true }) satisfies StoredDecision,
        decidedVia: "autopilot",
        decidedAt: new Date(),
      })
      .returning();

    if (input.stats) await mergeRunStats(input.runId, input.stats);

    await emit({
      userId: input.userId,
      runId: input.runId,
      kind: "approval.auto",
      message: input.messages?.auto ?? `Autopilot approved: ${input.title}.`,
      approvalId: approval.id,
      data: { kind: input.kind, ...input.data },
    });

    await enqueue(continuation);
    return approval;
  }

  const [approval] = await db
    .insert(approvals)
    .values({
      id,
      userId: input.userId,
      runId: input.runId,
      kind: input.kind,
      title: input.title,
      payload: input.payload,
      continuation,
      status: "pending",
      expiresAt: new Date(Date.now() + APPROVAL_TTL_MS),
    })
    .returning();

  // Conditional, so a cancel that landed first is not overwritten by the pause.
  const [paused] = await db
    .update(runs)
    .set({
      status: "awaiting_approval",
      stats: sql`${runs.stats} || ${JSON.stringify(input.stats ?? {})}::jsonb`,
      updatedAt: new Date(),
    })
    .where(
      and(
        eq(runs.id, input.runId),
        eq(runs.userId, input.userId),
        inArray(runs.status, ["queued", "running", "awaiting_approval"]),
      ),
    )
    .returning({ id: runs.id });

  if (!paused) {
    await db
      .update(approvals)
      .set({ status: "expired" })
      .where(and(eq(approvals.id, id), eq(approvals.status, "pending")));
    return { ...approval, status: "expired" };
  }

  await emit({
    userId: input.userId,
    runId: input.runId,
    kind: "approval.requested",
    message:
      input.messages?.requested ?? `Waiting for your go-ahead: ${input.title}.`,
    approvalId: approval.id,
    data: { kind: input.kind, ...input.data },
  });

  return approval;
}

// --- Deciding ------------------------------------------------------------------

export type DecideResult =
  | { ok: true; approval: Approval }
  | { ok: false; reason: "not_found" | "already_decided" | "expired" };

/**
 * Answers a pending approval, exactly once.
 *
 * The write is conditional on the row still being pending and unexpired, so a
 * double click, or the UI and chat answering at the same moment, decides it
 * once: the loser gets `already_decided` and enqueues nothing.
 *
 * `itemIds` is filtered to the payload's own items — unknown ids are ignored —
 * and approving an empty subset counts as a rejection.
 */
export async function decideApproval(
  userId: string,
  approvalId: string,
  decision: ApprovalDecision,
  via: "ui" | "chat",
): Promise<DecideResult> {
  if (!UUID.test(approvalId)) return { ok: false, reason: "not_found" };

  const current = await getApprovalRow(userId, approvalId);
  if (!current) return { ok: false, reason: "not_found" };
  if (current.status === "expired") return { ok: false, reason: "expired" };
  if (current.status !== "pending") {
    return { ok: false, reason: "already_decided" };
  }

  if (isPastExpiry(current)) {
    await expireStaleApprovals(userId);
    return { ok: false, reason: "expired" };
  }

  // A run that was cancelled or has finished cannot be resumed, whatever the
  // approval row still says.
  const [run] = await db
    .select({ status: runs.status })
    .from(runs)
    .where(eq(runs.id, current.runId))
    .limit(1);
  if (!run || !ACTIVE_RUN_STATUSES.includes(run.status)) {
    await db
      .update(approvals)
      .set({ status: "expired" })
      .where(and(eq(approvals.id, current.id), eq(approvals.status, "pending")));
    return { ok: false, reason: "expired" };
  }

  const stored = resolveDecision(current.payload, decision);
  const status = stored.approved ? "approved" : "rejected";

  const [decided] = await db
    .update(approvals)
    .set({
      status,
      decision: stored,
      decidedVia: via,
      decidedAt: new Date(),
    })
    .where(
      and(
        eq(approvals.id, approvalId),
        eq(approvals.userId, userId),
        eq(approvals.status, "pending"),
        or(isNull(approvals.expiresAt), gt(approvals.expiresAt, sql`now()`)),
      ),
    )
    .returning();

  if (!decided) {
    // Lost a race between the read above and this write: someone else
    // answered it, a cancel expired it, or its time ran out in between.
    const now = await getApprovalRow(userId, approvalId);
    if (!now) return { ok: false, reason: "not_found" };
    const lapsed =
      now.status === "expired" || (now.status === "pending" && isPastExpiry(now));
    return { ok: false, reason: lapsed ? "expired" : "already_decided" };
  }

  // The event goes out before the continuation is enqueued: the worker picks
  // a job up within milliseconds, and the feed should read "approved" before
  // it reads "evaluating".
  await emit({
    userId,
    runId: decided.runId,
    kind: "approval.decided",
    message: describeDecision(decided, stored),
    approvalId: decided.id,
    data: {
      kind: decided.kind,
      via,
      approved: stored.approved,
      itemIds: stored.itemIds ?? null,
    },
  });

  try {
    await enqueue(decided.continuation);
  } catch (error) {
    // Without its continuation the run would stay paused forever, so the
    // decision is undone and the caller told; answering again retries.
    await db
      .update(approvals)
      .set({ status: "pending", decision: null, decidedVia: null, decidedAt: null })
      .where(and(eq(approvals.id, decided.id), eq(approvals.status, status)));
    await emit({
      userId,
      runId: decided.runId,
      kind: "approval.reopened",
      level: "warn",
      message: "Could not resume the run, so the request is open again.",
      approvalId: decided.id,
    }).catch(() => undefined);
    throw error;
  }

  return { ok: true, approval: decided };
}

/**
 * The effective decision for a payload — pure, so the subset rules can be
 * tested without a database.
 */
export function resolveDecision(
  payload: unknown,
  decision: ApprovalDecision,
): StoredDecision {
  if (!hasItems(payload)) return { approved: decision.approved };
  if (!decision.approved) return { approved: false, itemIds: [] };

  const all = payloadItemIds(payload);
  const wanted = decision.itemIds ? new Set(decision.itemIds) : null;
  const itemIds = wanted ? all.filter((id) => wanted.has(id)) : all;

  return itemIds.length > 0
    ? { approved: true, itemIds }
    : { approved: false, itemIds: [] };
}

/** Whether a decided approval said yes. Anything else — rejected, expired, still pending — is a no. */
export function isApproved(approval: Pick<Approval, "status" | "decision">) {
  if (approval.status !== "approved" && approval.status !== "auto_approved") {
    return false;
  }
  return (approval.decision as StoredDecision | null)?.approved !== false;
}

/** The items a decided approval covers: the approved subset, or every item. */
export function approvedItemIds(
  approval: Pick<Approval, "payload" | "decision">,
): string[] {
  const decision = approval.decision as StoredDecision | null;
  if (Array.isArray(decision?.itemIds)) {
    return decision.itemIds.filter((id) => typeof id === "string");
  }
  return payloadItemIds(approval.payload);
}

/** Item ids in payload order: `matchId`, else `id`. */
export function payloadItemIds(payload: unknown): string[] {
  if (!hasItems(payload)) return [];
  const ids: string[] = [];
  for (const item of payload.items) {
    const record = item as Record<string, unknown> | null;
    const id = record?.matchId ?? record?.id;
    if (typeof id === "string" && !ids.includes(id)) ids.push(id);
  }
  return ids;
}

function hasItems(payload: unknown): payload is { items: unknown[] } {
  return (
    typeof payload === "object" &&
    payload !== null &&
    Array.isArray((payload as { items?: unknown }).items)
  );
}

/** What the feed says a decision was, e.g. "Approved deep evaluation of 12 of 15 roles." */
export function describeDecision(
  approval: Pick<Approval, "kind" | "title" | "payload">,
  decision: StoredDecision,
): string {
  const total = payloadItemIds(approval.payload).length;
  const chosen = decision.itemIds?.length ?? total;
  const count =
    chosen === total ? roles(chosen) : `${chosen} of ${roles(total)}`;

  switch (approval.kind) {
    case "deep_eval_batch":
      return decision.approved
        ? `Approved deep evaluation of ${count}.`
        : "Skipped the deep evaluation.";
    case "shortlist_proposal":
      return decision.approved
        ? `Approved shortlisting ${count}.`
        : "Left the shortlist as it was.";
    default:
      return decision.approved
        ? `Approved: ${approval.title}.`
        : `Declined: ${approval.title}.`;
  }
}

function roles(n: number) {
  return `${n} ${n === 1 ? "role" : "roles"}`;
}

// --- Reading -------------------------------------------------------------------

/**
 * Approvals waiting on this user, newest first.
 *
 * Only ones that can still be answered: unexpired, on a run that is still
 * paused. Anything past its expiry is expired on the way through, which also
 * resumes its run so the run can finish rather than wait forever.
 */
export async function listPendingApprovals(userId: string): Promise<Approval[]> {
  try {
    await expireStaleApprovals(userId);
  } catch (error) {
    // Housekeeping; the read below already excludes expired rows.
    console.error("[approvals] expiring stale approvals failed:", error);
  }

  const rows = await db
    .select({ approval: approvals })
    .from(approvals)
    .innerJoin(runs, eq(runs.id, approvals.runId))
    .where(
      and(
        eq(approvals.userId, userId),
        eq(approvals.status, "pending"),
        or(isNull(approvals.expiresAt), gt(approvals.expiresAt, sql`now()`)),
        inArray(runs.status, ACTIVE_RUN_STATUSES),
      ),
    )
    .orderBy(desc(approvals.createdAt));

  return rows.map((row) => row.approval);
}

/**
 * One approval. A pending row past its expiry reads as `expired`, so nothing
 * downstream offers to answer it.
 */
export async function getApproval(
  userId: string,
  id: string,
): Promise<Approval | null> {
  if (!UUID.test(id)) return null;
  const row = await getApprovalRow(userId, id);
  if (!row) return null;
  return row.status === "pending" && isPastExpiry(row)
    ? { ...row, status: "expired" }
    : row;
}

/** Every approval on one run, oldest first — for the run card's history. */
export async function listRunApprovals(
  userId: string,
  runId: string,
): Promise<Approval[]> {
  if (!UUID.test(runId)) return [];
  const rows = await db
    .select()
    .from(approvals)
    .where(and(eq(approvals.userId, userId), eq(approvals.runId, runId)))
    .orderBy(approvals.createdAt);
  return rows.map((row) =>
    row.status === "pending" && isPastExpiry(row)
      ? { ...row, status: "expired" as const }
      : row,
  );
}

async function getApprovalRow(
  userId: string,
  id: string,
): Promise<Approval | null> {
  const [row] = await db
    .select()
    .from(approvals)
    .where(and(eq(approvals.id, id), eq(approvals.userId, userId)))
    .limit(1);
  return row ?? null;
}

// --- Expiry --------------------------------------------------------------------

/**
 * Expires a run's open approvals — called when the run is cancelled, so its
 * cards stop being answerable. No continuation: there is nothing to resume.
 * Returns how many were expired.
 */
export async function expireRunApprovals(runId: string): Promise<number> {
  const expired = await db
    .update(approvals)
    .set({ status: "expired" })
    .where(and(eq(approvals.runId, runId), eq(approvals.status, "pending")))
    .returning({ id: approvals.id });
  return expired.length;
}

/**
 * Expires this user's pending approvals that are past their expiry.
 *
 * An expiry is a "no" by timeout, so it enqueues the continuation exactly as a
 * rejection does, and the continuation finishes the run. Returns how many
 * were expired.
 */
export async function expireStaleApprovals(userId: string): Promise<number> {
  const expired = await db
    .update(approvals)
    .set({ status: "expired" })
    .where(
      and(
        eq(approvals.userId, userId),
        eq(approvals.status, "pending"),
        lte(approvals.expiresAt, sql`now()`),
      ),
    )
    .returning();

  for (const approval of expired) {
    await emit({
      userId,
      runId: approval.runId,
      kind: "approval.expired",
      level: "warn",
      message: `No answer within a week, so this lapsed: ${approval.title}.`,
      approvalId: approval.id,
    });
    try {
      await enqueue(approval.continuation);
    } catch (error) {
      console.error(`[approvals] could not resume run ${approval.runId}:`, error);
    }
  }

  return expired.length;
}

function isPastExpiry(approval: Pick<Approval, "expiresAt">): boolean {
  return (
    approval.expiresAt !== null && approval.expiresAt.getTime() <= Date.now()
  );
}

// --- Internals -----------------------------------------------------------------

const ACTIVE_RUN_STATUSES: Array<(typeof runs.status.enumValues)[number]> = [
  "queued",
  "running",
  "awaiting_approval",
];

const QUEUE_NAMES = new Set<string>(Object.values(QUEUES));

/** Validates a stored continuation before enqueueing it verbatim. */
function parseContinuation(value: unknown): Continuation | null {
  if (typeof value !== "object" || value === null) return null;
  const { queue, payload } = value as { queue?: unknown; payload?: unknown };
  if (typeof queue !== "string" || !QUEUE_NAMES.has(queue)) return null;
  if (typeof payload !== "object" || payload === null) return null;
  return { queue, payload } as Continuation;
}

async function enqueue(value: unknown): Promise<void> {
  const continuation = parseContinuation(value);
  if (!continuation) {
    throw new Error("This approval has no valid continuation to resume.");
  }
  await send(continuation.queue, continuation.payload as never);
}

async function mergeRunStats(runId: string, patch: Record<string, unknown>) {
  await db
    .update(runs)
    .set({
      stats: sql`${runs.stats} || ${JSON.stringify(patch)}::jsonb`,
      updatedAt: new Date(),
    })
    .where(eq(runs.id, runId));
}

const UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
