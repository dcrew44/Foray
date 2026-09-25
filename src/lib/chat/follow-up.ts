import { consumeStream, createAgentUIStream, generateId } from "ai";
import { and, desc, eq, sql } from "drizzle-orm";

import { chatAgent, type ForayUIMessage } from "@/lib/agents/chat-agent";
import { db } from "@/lib/db";
import {
  getConversation,
  loadMessages,
  saveMessages,
} from "@/lib/db/queries/conversations";
import { getMatchRows } from "@/lib/db/queries/matches";
import {
  agentEvents,
  messages,
  user,
  type AgentEvent,
  type Approval,
  type Run,
} from "@/lib/db/schema";
import { withLlmContext } from "@/lib/llm/context";
import type { ChatFollowUpPayload } from "@/lib/queue/queues";
import { describeBrief, parseBrief } from "@/lib/ranking/brief";
import { getApproval, payloadItemIds } from "@/lib/runs/approvals";
import { emit } from "@/lib/runs/events";
import { getRun } from "@/lib/runs/runs";

import { FOLLOW_UP_EVENT } from "./follow-up-kinds";

/**
 * The agent reporting back.
 *
 * A chat turn ends when the agent starts a run; the run takes minutes, and
 * the agent used to fall silent while the card above it moved on without
 * comment: no summary when a search paused for a decision, nothing when it
 * finished. Now, when a run started from a conversation pauses, finishes or
 * fails (FOLLOW_UP_KINDS in ./follow-up-kinds.ts), the worker adds two messages
 * to that conversation: an automatic update describing the moment, and the
 * agent's reply to it, written with read-only tools so it can show results
 * but never act on the user's behalf.
 *
 * The update is stored as a user-role message marked `run_update` in its
 * metadata. That is what lets every later turn see it in context, and the
 * chat renders it as a quiet divider rather than as something the user said.
 */

/** What `messages.metadata` holds for the automatic update. */
export type RunUpdateMetadata = {
  kind: "run_update";
  runId: string;
  eventId: number;
  /** Shown in the chat as the divider's text, e.g. "Search finished". */
  label: string;
};

/** What `messages.metadata` holds for the agent's reply to it. */
export type FollowUpMetadata = { kind: "follow_up"; runId: string; eventId: number };


/** How long a follow-up waits for a reply the user is already getting. */
const REPLY_WAIT_MS = 90_000;
const REPLY_POLL_MS = 1_500;

export async function runChatFollowUp(
  payload: ChatFollowUpPayload,
): Promise<"posted" | "skipped"> {
  const { userId, runId, eventId } = payload;

  const [event] = await db
    .select()
    .from(agentEvents)
    .where(and(eq(agentEvents.id, eventId), eq(agentEvents.userId, userId)))
    .limit(1);
  const run = await getRun(userId, runId);
  if (!event || !run?.conversationId) return "skipped";

  const conversation = await getConversation(userId, run.conversationId);
  if (!conversation) return "skipped";

  const skip = async () => {
    // An open chat may be showing "looking at the results"; this clears it.
    await announce(userId, conversation.id, runId, { skipped: true });
    return "skipped" as const;
  };

  // News that is already stale is not worth a message: the user answered the
  // decision (from the card, most likely) or stopped the run before the
  // worker got here.
  if (run.status === "cancelled") return skip();
  const approval = event.approvalId ? await getApproval(userId, event.approvalId) : null;
  if (event.kind === "approval.requested" && approval?.status !== "pending") return skip();
  if (await alreadyFollowedUp(conversation.id, eventId)) return skip();

  // The user's own message can be what finished the run ("yes, shortlist
  // both" completes a search in seconds). Writing now would put this update
  // above the reply they are still receiving, and write it without knowing
  // what that reply says. So wait for that reply to be saved first. (Waiting
  // here rather than re-queueing: a delayed job waits for the worker's slow
  // poll, which added half a minute.)
  const waitUntil = Date.now() + REPLY_WAIT_MS;
  while ((await replyInFlight(conversation.id)) && Date.now() < waitUntil) {
    await new Promise((resolve) => setTimeout(resolve, REPLY_POLL_MS));
  }

  // At a gate, the roles in question go into the update itself: the approval
  // card already lists them, so the reply should talk about them rather than
  // call list_jobs and render the same list again.
  const proposed = approval ? await proposedRoles(userId, approval) : [];
  const update = describeUpdate(run, event, approval, proposed);
  const notice: ForayUIMessage = {
    id: generateId(),
    role: "user",
    parts: [{ type: "text", text: update.text }],
    metadata: { kind: "run_update", runId, eventId, label: update.label } satisfies RunUpdateMetadata,
  };

  // Persisted parts are this server's own output, re-typed rather than
  // revalidated, as in the chat route.
  const history = (await loadMessages(userId, conversation.id)) as ForayUIMessage[];
  await saveMessages(userId, conversation.id, [notice]);

  try {
    const [person] = await db
      .select({ name: user.name })
      .from(user)
      .where(eq(user.id, userId))
      .limit(1);
    const agent = await chatAgent(userId, {
      conversationId: conversation.id,
      userName: person?.name ?? null,
      followUp: true,
    });

    const uiMessages = [...history, notice];
    let reply: ForayUIMessage | null = null;

    await withLlmContext({ userId }, async () => {
      const stream = await createAgentUIStream({
        agent,
        uiMessages,
        originalMessages: uiMessages,
        timeout: 90_000,
        generateMessageId: generateId,
        messageMetadata: () =>
          ({ kind: "follow_up", runId, eventId }) satisfies FollowUpMetadata,
        onEnd: ({ responseMessage }) => {
          reply = responseMessage ?? null;
        },
      });
      // Nobody watches this stream: reading it to the end is what runs the
      // agent, and `onEnd` hands over the finished message. An error inside
      // it is rethrown, so a failed reply is not saved as an empty one.
      let failure: unknown = null;
      await consumeStream({ stream, onError: (error) => (failure = error) });
      if (failure) throw failure;
    });

    const finished = reply as ForayUIMessage | null;
    if (!finished || !hasContent(finished)) throw new Error("The follow-up came back empty.");

    await saveMessages(userId, conversation.id, [finished]);
    await announce(userId, conversation.id, runId, { messageId: finished.id, label: update.label });
    return "posted";
  } catch (error) {
    console.error(`[follow-up] run ${runId}, event ${eventId}:`, error);
    // An update with no reply would sit in the history as an unanswered
    // prompt, and the next turn would answer it out of the blue.
    await db.delete(messages).where(eq(messages.id, notice.id));
    await announce(userId, conversation.id, runId, { failed: true });
    return "skipped";
  }
}

async function proposedRoles(userId: string, approval: Approval): Promise<ProposedRole[]> {
  const ids = payloadItemIds(approval.payload).slice(0, 10);
  const rows = await getMatchRows(userId, ids);
  const order = new Map(ids.map((id, index) => [id, index]));
  return rows
    .sort((a, b) => (order.get(a.matchId) ?? 0) - (order.get(b.matchId) ?? 0))
    .map((row) => ({
      title: row.title.trim(),
      company: row.company,
      score: row.deepScore ?? row.fastScore,
      headline: row.headline,
      locations: row.locations.slice(0, 3).join(", "),
    }));
}

/**
 * Whether the conversation's last message is the user's, recent, and not yet
 * answered: a chat turn is streaming and will be saved when it ends. Exported
 * for tests.
 */
export async function replyInFlight(conversationId: string): Promise<boolean> {
  const [last] = await db
    .select({ role: messages.role, metadata: messages.metadata, createdAt: messages.createdAt })
    .from(messages)
    .where(eq(messages.conversationId, conversationId))
    .orderBy(desc(messages.createdAt))
    .limit(1);
  if (!last || last.role !== "user") return false;
  if ((last.metadata as { kind?: unknown } | null)?.kind === "run_update") return false;
  return Date.now() - last.createdAt.getTime() < REPLY_WAIT_MS;
}

async function alreadyFollowedUp(conversationId: string, eventId: number) {
  const [row] = await db
    .select({ id: messages.id })
    .from(messages)
    .where(
      and(
        eq(messages.conversationId, conversationId),
        sql`${messages.metadata}->>'kind' = 'run_update'`,
        sql`(${messages.metadata}->>'eventId')::bigint = ${eventId}`,
      ),
    )
    .limit(1);
  return Boolean(row);
}

function hasContent(message: ForayUIMessage) {
  return message.parts.some(
    (part) => (part.type === "text" && part.text.trim().length > 0) || part.type.startsWith("tool-"),
  );
}

async function announce(
  userId: string,
  conversationId: string,
  runId: string,
  data: Record<string, unknown>,
) {
  await emit({
    userId,
    kind: FOLLOW_UP_EVENT,
    level: "debug",
    message: data.messageId ? "Foray followed up in chat." : "No chat follow-up was needed.",
    data: { conversationId, runId, ...data },
  });
}

// --- What the agent is told ----------------------------------------------------

type Update = { label: string; text: string };

/** A role a gate is asking about, as the update describes it. */
export type ProposedRole = {
  title: string;
  company: string;
  score: number | null;
  headline: string | null;
  locations: string;
};

function roleLines(roles: ProposedRole[]): string {
  return roles
    .map(
      (role) =>
        `- ${role.title} at ${role.company}` +
        (role.score != null ? ` (${role.score})` : "") +
        (role.locations ? `, ${role.locations}` : "") +
        (role.headline ? `: ${role.headline}` : ""),
    )
    .join("\n");
}

const RULES =
  "Keep it short and conversational: a few sentences, no headings. Do not repeat what the " +
  "conversation above already says, and do not restate numbers the run card shows unless " +
  "they matter. In this reply you can only look things up: do not say you started, " +
  "approved, moved or changed anything.";

/**
 * The automatic update: what happened, in plain words, and what a useful
 * report back looks like for this kind of moment. Exported for tests.
 */
export function describeUpdate(
  run: Pick<Run, "id" | "kind" | "input" | "output">,
  event: Pick<AgentEvent, "kind" | "message">,
  approval: Pick<Approval, "kind" | "title" | "payload"> | null,
  proposed: ProposedRole[] = [],
): Update {
  const input = (run.input ?? {}) as { prompt?: string | null; brief?: unknown; hints?: unknown };
  const what =
    run.kind === "search"
      ? (describeBrief(parseBrief(input.brief ?? input.hints)) ?? input.prompt ?? "a search")
      : run.kind === "tailor"
        ? (input.prompt?.replace(/^Tailor /, "tailoring ") ?? "tailoring a resume")
        : (input.prompt ?? KIND_NAME[run.kind] ?? "background work");
  const head = `[Automatic update about run ${run.id}, not written by the user]`;
  const listIt = `Call list_jobs with run_id "${run.id}" to see them; the user sees those as cards, so do not retype their details.`;

  if (event.kind === "approval.requested" && approval?.kind === "deep_eval_batch") {
    return {
      label: "Search paused for your go-ahead",
      text: [
        head,
        `The search (${what}) has scored what it found and is paused, waiting for the user: ${approval.title}. ${event.message}`,
        proposed.length ? `The roles it proposes to review, best first (fast score in brackets):\n${roleLines(proposed)}` : null,
        "The approval card above already lists these roles, so do not call list_jobs. In two or three sentences, say what stands out: which look strongest and why, and anything that gives you pause. Then ask whether to go ahead with the deep review. Do not answer the approval yourself.",
        RULES,
      ]
        .filter(Boolean)
        .join("\n"),
    };
  }

  if (event.kind === "approval.requested" && approval?.kind === "shortlist_proposal") {
    const items =
      proposed.length > 0
        ? proposed
        : ((approval.payload as { items?: Array<{ title?: string; company?: string }> })?.items ?? []).map(
            (item) => ({ title: item.title ?? "", company: item.company ?? "", score: null, headline: null, locations: "" }),
          );
    return {
      label: "Deep review done",
      text: [
        head,
        `The deep review for the search (${what}) is done. It proposes shortlisting ${items.length} ${items.length === 1 ? "role" : "roles"}, with the review's score and verdict:\n${roleLines(items)}`,
        "The approval card above already lists these roles, so do not call list_jobs. Say which look best and why, grounded in the reviews (get_job has the evidence if you need more), then ask whether to shortlist them. Do not answer the approval yourself.",
        RULES,
      ].join("\n"),
    };
  }

  if (event.kind === "approval.requested") {
    return {
      label: "Waiting for your go-ahead",
      text: [head, `${what} is paused, waiting for the user: ${approval?.title ?? event.message}.`, "Say what it needs and ask.", RULES].join("\n"),
    };
  }

  if (event.kind === "run.failed" || event.kind === "tailor.failed") {
    return {
      label: `${KIND_TITLE[run.kind] ?? "Run"} stopped`,
      text: [
        head,
        `${capitalize(what)} stopped with an error: ${event.message}`,
        "Explain what happened in a sentence, without technical detail, and offer to try again.",
        RULES,
      ].join("\n"),
    };
  }

  if (run.kind === "search") {
    const output = (run.output ?? {}) as { candidates?: number };
    const empty = (output.candidates ?? 0) === 0;
    return {
      label: "Search finished",
      text: [
        head,
        `The search (${what}) has finished. Its result: ${event.message}`,
        empty
          ? "Nothing matched. Say so plainly and suggest one or two ways to widen the search."
          : `${listIt} Show the best few (limit 5), then say in one or two sentences what stands out.`,
        "End with one concrete next step you could take for them.",
        RULES,
      ].join("\n"),
    };
  }

  if (run.kind === "rank_deep") {
    return {
      label: "Deep review finished",
      text: [
        head,
        `The deep review has finished. Its result: ${event.message}`,
        "Give the verdict on each reviewed role in a sentence or two, grounded in the review (get_job with its match id has the evidence), and suggest a next step.",
        RULES,
      ].join("\n"),
    };
  }

  if (run.kind === "tailor") {
    return {
      label: "Tailored resume ready",
      text: [
        head,
        `${capitalize(what)} is done. ${event.message}`,
        "Tell them it is ready (the card above has the PDF), mention anything flagged for their review, and suggest what to do next.",
        RULES,
      ].join("\n"),
    };
  }

  return {
    label: `${KIND_TITLE[run.kind] ?? "Run"} finished`,
    text: [head, `${capitalize(what)} has finished: ${event.message}`, "Report the result briefly.", RULES].join("\n"),
  };
}

const KIND_NAME: Record<string, string> = {
  search: "a search",
  rank_deep: "a deep review",
  tailor: "tailoring a resume",
  resume_render: "rendering a resume",
  company_discovery: "company discovery",
};

const KIND_TITLE: Record<string, string> = {
  search: "Search",
  rank_deep: "Deep review",
  tailor: "Tailoring",
  resume_render: "Rendering",
  company_discovery: "Discovery",
};

function capitalize(text: string) {
  return text.charAt(0).toUpperCase() + text.slice(1);
}
