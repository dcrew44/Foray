import { randomUUID } from "node:crypto";

import { and, eq } from "drizzle-orm";
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

// Nothing may reach the real queue: a live worker would pick the continuation
// up and resume a run that belongs to a test.
vi.mock("@/lib/queue/send", () => ({ send: vi.fn(async () => "job-id") }));

import { db } from "@/lib/db";
import { agentEvents, approvals, runs, user } from "@/lib/db/schema";
import { send } from "@/lib/queue/send";
import {
  APPROVAL_TTL_MS,
  approvedItemIds,
  decideApproval,
  getApproval,
  isApproved,
  listPendingApprovals,
  requestApproval,
  resolveDecision,
  type DeepEvalBatchPayload,
} from "@/lib/runs/approvals";
import { createRun, requestCancel } from "@/lib/runs/runs";

/**
 * The approval gate's state machine, against the real database with a
 * throwaway user (every per-user table cascades from it, so deleting the user
 * afterwards removes everything these tests wrote).
 */

const userId = `test-approvals-${randomUUID()}`;
const sent = vi.mocked(send);

beforeAll(async () => {
  await db
    .insert(user)
    .values({ id: userId, name: "Approvals test", email: `${userId}@example.test` });
});

afterAll(async () => {
  await db.delete(user).where(eq(user.id, userId));
});

beforeEach(() => {
  sent.mockClear();
});

function batchPayload(): DeepEvalBatchPayload {
  return {
    items: [
      { matchId: randomUUID(), jobId: "test:acme:1", title: "Engineer", company: "Acme", fastScore: 88 },
      { matchId: randomUUID(), jobId: "test:beta:2", title: "Developer", company: "Beta", fastScore: 74 },
      { matchId: randomUUID(), jobId: "test:gamma:3", title: "SWE", company: "Gamma", fastScore: 61 },
    ],
    estimate: { usd: 0.15, perItemUsd: 0.05, model: "openai:gpt-5.6-terra" },
    note: null,
  };
}

async function pausedAt(
  mode: "approve_each" | "autopilot" = "approve_each",
  payload: DeepEvalBatchPayload = batchPayload(),
) {
  const run = await createRun({ userId, kind: "search", status: "running", mode });
  const approval = await requestApproval({
    userId,
    runId: run.id,
    kind: "deep_eval_batch",
    title: "Deep-evaluate 3 roles (about $0.15)",
    payload,
    mode,
    continuation: (approvalId) => ({
      queue: "search.evaluate",
      payload: { userId, runId: run.id, approvalId },
    }),
    stats: { phase: "awaiting_evaluation", toEvaluate: 3 },
  });
  return { run, approval, payload };
}

async function runRow(id: string) {
  const [row] = await db.select().from(runs).where(eq(runs.id, id));
  return row;
}

async function eventsFor(runId: string) {
  return db
    .select()
    .from(agentEvents)
    .where(and(eq(agentEvents.userId, userId), eq(agentEvents.runId, runId)))
    .orderBy(agentEvents.id);
}

describe("requestApproval", () => {
  it("in approve_each, writes a pending approval, pauses the run and announces it", async () => {
    const { run, approval } = await pausedAt();

    expect(approval.status).toBe("pending");
    expect(approval.decision).toBeNull();
    expect(approval.expiresAt!.getTime() - Date.now()).toBeGreaterThan(APPROVAL_TTL_MS - 60_000);
    // The continuation knows which approval it resumes from.
    expect(approval.continuation).toEqual({
      queue: "search.evaluate",
      payload: { userId, runId: run.id, approvalId: approval.id },
    });

    const paused = await runRow(run.id);
    expect(paused.status).toBe("awaiting_approval");
    expect(paused.stats).toMatchObject({ phase: "awaiting_evaluation", toEvaluate: 3 });

    const events = await eventsFor(run.id);
    expect(events.map((event) => event.kind)).toEqual(["approval.requested"]);
    expect(events[0].approvalId).toBe(approval.id);

    expect(sent).not.toHaveBeenCalled();
  });

  it("on autopilot, approves every item itself and enqueues the continuation at once", async () => {
    const { run, approval, payload } = await pausedAt("autopilot");

    expect(approval.status).toBe("auto_approved");
    expect(approval.decidedVia).toBe("autopilot");
    expect(approval.decision).toEqual({
      approved: true,
      itemIds: payload.items.map((item) => item.matchId),
    });
    expect(isApproved(approval)).toBe(true);

    expect(sent).toHaveBeenCalledTimes(1);
    expect(sent).toHaveBeenCalledWith("search.evaluate", {
      userId,
      runId: run.id,
      approvalId: approval.id,
    });

    // The run is never paused on autopilot.
    expect((await runRow(run.id)).status).toBe("running");
    const kinds = (await eventsFor(run.id)).map((event) => event.kind);
    expect(kinds).toEqual(["approval.auto"]);
  });

  it("expires the approval on the spot if the run was cancelled first", async () => {
    const run = await createRun({ userId, kind: "search", status: "cancelled" });
    const approval = await requestApproval({
      userId,
      runId: run.id,
      kind: "deep_eval_batch",
      title: "Too late",
      payload: batchPayload(),
      mode: "approve_each",
      continuation: (approvalId) => ({
        queue: "search.evaluate",
        payload: { userId, runId: run.id, approvalId },
      }),
    });

    expect(approval.status).toBe("expired");
    expect((await getApproval(userId, approval.id))?.status).toBe("expired");
    expect((await runRow(run.id)).status).toBe("cancelled");
  });
});

describe("decideApproval", () => {
  it("approves a subset, ignoring ids that are not items, and enqueues the continuation once", async () => {
    const { run, approval, payload } = await pausedAt();
    const [first, , third] = payload.items;

    const result = await decideApproval(
      userId,
      approval.id,
      { approved: true, itemIds: [third.matchId, "not-an-item", first.matchId] },
      "ui",
    );

    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.approval.status).toBe("approved");
    expect(result.approval.decidedVia).toBe("ui");
    // Payload order, unknown ids dropped.
    expect(result.approval.decision).toEqual({
      approved: true,
      itemIds: [first.matchId, third.matchId],
    });
    expect(approvedItemIds(result.approval)).toEqual([first.matchId, third.matchId]);

    expect(sent).toHaveBeenCalledTimes(1);
    expect(sent).toHaveBeenCalledWith("search.evaluate", {
      userId,
      runId: run.id,
      approvalId: approval.id,
    });

    const decided = (await eventsFor(run.id)).find((e) => e.kind === "approval.decided");
    expect(decided?.message).toBe("Approved deep evaluation of 2 of 3 roles.");
    expect(decided?.approvalId).toBe(approval.id);
  });

  it("approves everything when no subset is given", async () => {
    const { approval, payload } = await pausedAt();
    const result = await decideApproval(userId, approval.id, { approved: true }, "chat");

    expect(result.ok && result.approval.decision).toEqual({
      approved: true,
      itemIds: payload.items.map((item) => item.matchId),
    });
    expect(result.ok && result.approval.decidedVia).toBe("chat");
  });

  it("is a no-op the second time: already decided, nothing enqueued", async () => {
    const { approval } = await pausedAt();

    const first = await decideApproval(userId, approval.id, { approved: true }, "ui");
    const second = await decideApproval(userId, approval.id, { approved: false }, "chat");

    expect(first.ok).toBe(true);
    expect(second).toEqual({ ok: false, reason: "already_decided" });
    expect(sent).toHaveBeenCalledTimes(1);
    expect((await getApproval(userId, approval.id))?.status).toBe("approved");
  });

  it("decides exactly once when the UI and chat answer at the same moment", async () => {
    const { approval } = await pausedAt();

    const results = await Promise.all([
      decideApproval(userId, approval.id, { approved: true }, "ui"),
      decideApproval(userId, approval.id, { approved: false }, "chat"),
      decideApproval(userId, approval.id, { approved: true }, "chat"),
    ]);

    expect(results.filter((result) => result.ok)).toHaveLength(1);
    expect(sent).toHaveBeenCalledTimes(1);
  });

  it("treats approving an empty subset as a rejection, and still resumes the run", async () => {
    const { run, approval } = await pausedAt();

    const result = await decideApproval(
      userId,
      approval.id,
      { approved: true, itemIds: ["nothing-here"] },
      "ui",
    );

    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.approval.status).toBe("rejected");
    expect(result.approval.decision).toEqual({ approved: false, itemIds: [] });
    expect(isApproved(result.approval)).toBe(false);
    // The continuation is what finishes a rejected run.
    expect(sent).toHaveBeenCalledWith("search.evaluate", {
      userId,
      runId: run.id,
      approvalId: approval.id,
    });

    const decided = (await eventsFor(run.id)).find((e) => e.kind === "approval.decided");
    expect(decided?.message).toBe("Skipped the deep evaluation.");
  });

  it("will not decide an expired approval, and expiring it resumes the run as a no", async () => {
    const { run, approval } = await pausedAt();
    await db
      .update(approvals)
      .set({ expiresAt: new Date(Date.now() - 1000) })
      .where(eq(approvals.id, approval.id));

    // Reads treat it as expired before anything has swept it.
    expect((await getApproval(userId, approval.id))?.status).toBe("expired");
    expect((await listPendingApprovals(userId)).map((a) => a.id)).not.toContain(approval.id);

    sent.mockClear();
    const result = await decideApproval(userId, approval.id, { approved: true }, "ui");
    expect(result).toEqual({ ok: false, reason: "expired" });

    const [row] = await db.select().from(approvals).where(eq(approvals.id, approval.id));
    expect(row.status).toBe("expired");
    expect(row.decision).toBeNull();
    // No yes was sent anywhere: the only thing enqueued is nothing, because
    // listPendingApprovals above already swept it and resumed the run.
    expect(sent).not.toHaveBeenCalled();

    const kinds = (await eventsFor(run.id)).map((event) => event.kind);
    expect(kinds).toContain("approval.expired");
  });

  it("sweeps a lapsed approval into a resumed run exactly once", async () => {
    const { run, approval } = await pausedAt();
    await db
      .update(approvals)
      .set({ expiresAt: new Date(Date.now() - 1000) })
      .where(eq(approvals.id, approval.id));

    await listPendingApprovals(userId);
    await listPendingApprovals(userId);

    const resumes = sent.mock.calls.filter(
      ([, payload]) => (payload as { approvalId?: string }).approvalId === approval.id,
    );
    expect(resumes).toEqual([
      ["search.evaluate", { userId, runId: run.id, approvalId: approval.id }],
    ]);
  });

  it("reports unknown and malformed ids as not found", async () => {
    expect(await decideApproval(userId, randomUUID(), { approved: true }, "ui")).toEqual({
      ok: false,
      reason: "not_found",
    });
    expect(await decideApproval(userId, "approval-7", { approved: true }, "chat")).toEqual({
      ok: false,
      reason: "not_found",
    });
  });

  it("will not let another user decide", async () => {
    const { approval } = await pausedAt();
    expect(
      await decideApproval(`someone-else-${randomUUID()}`, approval.id, { approved: true }, "ui"),
    ).toEqual({ ok: false, reason: "not_found" });
    expect(sent).not.toHaveBeenCalled();
  });
});

describe("cancelling a paused run", () => {
  it("expires its open approvals, reports the cancel, and leaves nothing to answer", async () => {
    const { run, approval } = await pausedAt();

    const cancelled = await requestCancel(userId, run.id);
    expect(cancelled?.status).toBe("cancelled");

    expect((await getApproval(userId, approval.id))?.status).toBe("expired");
    expect((await listPendingApprovals(userId)).map((a) => a.id)).not.toContain(approval.id);
    expect(await decideApproval(userId, approval.id, { approved: true }, "ui")).toEqual({
      ok: false,
      reason: "expired",
    });
    expect(sent).not.toHaveBeenCalled();

    const kinds = (await eventsFor(run.id)).map((event) => event.kind);
    expect(kinds).toContain("run.cancelled");
  });

  it("lists only approvals that can still be answered", async () => {
    const open = await pausedAt();
    const answered = await pausedAt();
    await decideApproval(userId, answered.approval.id, { approved: false }, "ui");

    const pending = (await listPendingApprovals(userId)).map((a) => a.id);
    expect(pending).toContain(open.approval.id);
    expect(pending).not.toContain(answered.approval.id);
  });
});

describe("resolveDecision", () => {
  const payload = {
    items: [{ matchId: "a" }, { matchId: "b" }, { id: "c" }],
  };

  it("keeps payload order and drops unknown ids", () => {
    expect(resolveDecision(payload, { approved: true, itemIds: ["c", "zzz", "a", "a"] })).toEqual({
      approved: true,
      itemIds: ["a", "c"],
    });
  });

  it("approves everything without a subset, and nothing on a rejection", () => {
    expect(resolveDecision(payload, { approved: true })).toEqual({
      approved: true,
      itemIds: ["a", "b", "c"],
    });
    expect(resolveDecision(payload, { approved: false, itemIds: ["a"] })).toEqual({
      approved: false,
      itemIds: [],
    });
  });

  it("counts an empty approved subset as a rejection", () => {
    expect(resolveDecision(payload, { approved: true, itemIds: [] })).toEqual({
      approved: false,
      itemIds: [],
    });
  });

  it("passes a plain yes or no through for payloads without items", () => {
    expect(resolveDecision({ company: "Stripe" }, { approved: true })).toEqual({ approved: true });
    expect(resolveDecision(null, { approved: false })).toEqual({ approved: false });
  });
});
