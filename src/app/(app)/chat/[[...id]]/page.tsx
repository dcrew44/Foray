import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { Chat } from "@/components/chat/chat";
import type { ChatPage as ChatPageData, ConversationOut, ForayUIMessage } from "@/lib/api/chat-types";
import { django } from "@/lib/api/server";

export const metadata: Metadata = { title: "Chat" };

/** A conversation id: a UUID, as Django's path parameter is typed (§5.2). */
const CHAT_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * /chat starts a new thread; /chat/<id> resumes one.
 *
 * A new thread gets a fresh id on every visit and is keyed by it, so "New
 * chat" always remounts a clean conversation even when the address bar was
 * rewritten client-side by the previous one. The id is minted here, on the
 * server, per visit; Django claims it on the first message (§7.3).
 *
 * Each branch makes one Django read, which also checks the session: a 401
 * goes to sign in, and someone else's conversation is the same 404 as one
 * that does not exist.
 */
export default async function ChatPage({
  params,
}: {
  params: Promise<{ id?: string[] }>;
}) {
  const conversationId = (await params).id?.[0] ?? null;

  if (!conversationId) {
    const { suggestions } = await django<ChatPageData>("/api/pages/chat");
    const draftId = crypto.randomUUID();
    return (
      <Chat
        chatId={draftId}
        initialMessages={[]}
        isNew
        key={draftId}
        suggestions={suggestions}
        title={null}
      />
    );
  }

  // Django would answer 404 too; checking first keeps the id out of the
  // request path unless it is one.
  if (!CHAT_ID.test(conversationId)) notFound();

  const { conversation, messages } = await django<ConversationOut>(
    `/api/conversations/${conversationId}`,
  );

  return (
    <Chat
      chatId={conversation.id}
      // The rows are the stored UI messages, returned as they are (§7.4).
      initialMessages={messages as unknown as ForayUIMessage[]}
      isNew={false}
      key={conversation.id}
      suggestions={[]}
      title={conversation.title}
    />
  );
}
