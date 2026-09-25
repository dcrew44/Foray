import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { Chat, type ChatSuggestion } from "@/components/chat/chat";
import type { ForayUIMessage } from "@/lib/agents/chat-agent";
import { requireUser } from "@/lib/auth/session";
import { getConversation, loadMessages } from "@/lib/db/queries/conversations";
import { listMatches } from "@/lib/db/queries/matches";
import { getOnboardingState } from "@/lib/profile/onboarding";

export const metadata: Metadata = { title: "Chat" };

/**
 * /chat starts a new thread; /chat/<id> resumes one.
 *
 * A new thread gets a fresh id on every visit and is keyed by it, so "New
 * chat" always remounts a clean conversation even when the address bar was
 * rewritten client-side by the previous one.
 */
export default async function ChatPage({
  params,
}: {
  params: Promise<{ id?: string[] }>;
}) {
  const user = await requireUser();
  const conversationId = (await params).id?.[0] ?? null;

  if (!conversationId) {
    const draftId = crypto.randomUUID();
    return (
      <Chat
        chatId={draftId}
        initialMessages={[]}
        isNew
        key={draftId}
        suggestions={await suggestionsFor(user.id)}
        title={null}
      />
    );
  }

  // Someone else's conversation is indistinguishable from one that does not
  // exist: getConversation filters on user id.
  const conversation = await getConversation(user.id, conversationId);
  if (!conversation) notFound();

  const history = (await loadMessages(user.id, conversation.id)) as ForayUIMessage[];

  return (
    <Chat
      chatId={conversation.id}
      initialMessages={history}
      isNew={false}
      key={conversation.id}
      suggestions={[]}
      title={conversation.title}
    />
  );
}

/** Starting points that fit where this user is. */
async function suggestionsFor(userId: string): Promise<ChatSuggestion[]> {
  const onboarding = await getOnboardingState(userId);

  if (!onboarding.hasResume) {
    return [
      {
        title: "How does this work?",
        prompt: "What can you do, and what do you need from me to get started?",
      },
    ];
  }

  if (!onboarding.ready) {
    return [
      {
        title: "Set up my search",
        prompt: "Let's set up my job search. Ask me what you need to know.",
      },
      {
        title: "What's on my resume?",
        prompt: "Summarize my resume and tell me what kinds of roles it fits.",
      },
    ];
  }

  const [top] = await listMatches(userId, { stages: ["found", "shortlisted"], limit: 1 });
  const role = onboarding.preferences.target_roles?.[0]?.toLowerCase() ?? "roles";
  const where = onboarding.preferences.locations?.[0];

  return [
    {
      title: "Find new roles",
      prompt: `Find new ${role}${where ? ` in ${where}` : ""} for me.`,
    },
    {
      title: "Compare my best matches",
      prompt: "Which of my matches fit me best, and why? Be specific.",
    },
    top
      ? {
          title: "Tailor my resume",
          prompt: `Tailor my resume for the ${top.title} role at ${top.company}.`,
        }
      : {
          title: "Improve my resume",
          prompt: "What would make my resume stronger for the roles I want?",
        },
    {
      title: "Add a company",
      prompt: "Start tracking this company for me: ",
      prefill: true,
    },
  ];
}
