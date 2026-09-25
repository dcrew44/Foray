import { randomUUID } from "node:crypto";

import { eq } from "drizzle-orm";
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * When a follow-up is queued and when it holds back, against the real
 * database with a throwaway user. The queue is faked, and nothing here reaches
 * a model: every case stops before the agent would run.
 */

vi.mock("@/lib/queue/send", () => ({ send: vi.fn(async () => "job-id") }));

import { replyInFlight, runChatFollowUp } from "@/lib/chat/follow-up";
import { db } from "@/lib/db";
import { createConversation, saveMessages } from "@/lib/db/queries/conversations";
import { runs, user } from "@/lib/db/schema";
import { send } from "@/lib/queue/send";
import { emit } from "@/lib/runs/events";
import { createRun } from "@/lib/runs/runs";

const userId = `test-follow-up-${randomUUID()}`;
const sent = vi.mocked(send);

beforeAll(async () => {
  await db
    .insert(user)
    .values({ id: userId, name: "Follow-up test", email: `${userId}@example.test` });
});

afterAll(async () => {
  await db.delete(user).where(eq(user.id, userId));
});

beforeEach(() => sent.mockClear());

async function chatRun() {
  const conversation = await createConversation(userId, "Find roles");
  const run = await createRun({
    userId,
    kind: "search",
    status: "completed",
    input: { prompt: "roles at Anthropic" },
    output: { candidates: 3 },
    conversationId: conversation.id,
  });
  return { conversation, run };
}

function followUps() {
  return sent.mock.calls.filter(([queue]) => queue === "chat.follow_up");
}

describe("queueing a follow-up", () => {
  it("queues one when a run started from chat finishes", async () => {
    const { run } = await chatRun();
    const event = await emit({ userId, runId: run.id, kind: "run.completed", message: "Found 3 roles." });
    expect(followUps()).toEqual([
      ["chat.follow_up", { userId, runId: run.id, eventId: event.id }, { singletonKey: `follow-up-${event.id}` }],
    ]);
  });

  it("does not for a run started from a button, or for progress events", async () => {
    const buttonRun = await createRun({ userId, kind: "search", status: "completed", input: {} });
    await emit({ userId, runId: buttonRun.id, kind: "run.completed", message: "Found 3 roles." });

    const { run } = await chatRun();
    await emit({ userId, runId: run.id, kind: "scoring.progress", message: "Scored 3 of 9" });

    expect(followUps()).toEqual([]);
  });
});

describe("holding a follow-up back", () => {
  it("sees a reply still streaming, and waits for it", async () => {
    const { conversation } = await chatRun();
    expect(await replyInFlight(conversation.id)).toBe(false);

    await saveMessages(userId, conversation.id, [
      { id: randomUUID(), role: "user", parts: [{ type: "text", text: "Yes, shortlist both" }] },
    ]);
    expect(await replyInFlight(conversation.id)).toBe(true);

    await saveMessages(userId, conversation.id, [
      { id: randomUUID(), role: "assistant", parts: [{ type: "text", text: "Shortlisted both." }] },
    ]);
    expect(await replyInFlight(conversation.id)).toBe(false);
  });

  it("skips news that is already stale: a cancelled run", async () => {
    const { run } = await chatRun();
    const event = await emit({ userId, runId: run.id, kind: "run.completed", message: "Found 3 roles." });
    await db.update(runs).set({ status: "cancelled" }).where(eq(runs.id, run.id));

    expect(await runChatFollowUp({ userId, runId: run.id, eventId: event.id })).toBe("skipped");
  });

  it("skips an event that belongs to someone else", async () => {
    const { run } = await chatRun();
    const event = await emit({ userId, runId: run.id, kind: "run.completed", message: "Found 3 roles." });

    expect(await runChatFollowUp({ userId: "someone-else", runId: run.id, eventId: event.id })).toBe("skipped");
  });
});
