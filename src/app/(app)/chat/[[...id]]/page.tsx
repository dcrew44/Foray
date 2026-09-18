import { notFound } from "next/navigation";

import { Chat } from "@/components/chat/chat";
import type { ForayUIMessage } from "@/lib/agents/chat-agent";
import { requireUser } from "@/lib/auth/session";
import { getConversation, loadMessages } from "@/lib/db/queries/conversations";

/**
 * /chat starts a new thread; /chat/<id> resumes one.
 *
 * The history is read on the server and handed to the client as the initial
 * messages, so a reload shows the conversation without a flash of empty state.
 */
export default async function ChatPage({
  params,
}: {
  params: Promise<{ id?: string[] }>;
}) {
  const user = await requireUser();
  const conversationId = (await params).id?.[0] ?? null;

  if (!conversationId) {
    return <Chat conversationId={null} initialMessages={[]} />;
  }

  // Someone else's conversation is indistinguishable from one that does not
  // exist: getConversation filters on user id.
  const conversation = await getConversation(user.id, conversationId);
  if (!conversation) notFound();

  const history = (await loadMessages(
    user.id,
    conversation.id,
  )) as ForayUIMessage[];

  return <Chat conversationId={conversation.id} initialMessages={history} />;
}
