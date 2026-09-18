import { createAgentUIStreamResponse } from "ai";

import { withLlmContext } from "@/lib/llm/context";

import { chatAgent, type ForayUIMessage } from "@/lib/agents/chat-agent";
import { requireUser } from "@/lib/auth/session";
import {
  ensureConversation,
  loadMessages,
  saveMessages,
} from "@/lib/db/queries/conversations";

/**
 * One chat turn.
 *
 * The client sends only the newest message, not the whole thread: the history
 * lives in Postgres and is the authority. That keeps the request small however
 * long the conversation gets, and means the client cannot rewrite what was
 * already said.
 */
export async function POST(request: Request) {
  const user = await requireUser();

  const body = (await request.json()) as {
    id?: string;
    message?: ForayUIMessage;
    messages?: ForayUIMessage[];
  };

  // `message` is the single-message transport; `messages` is the SDK default,
  // accepted so the endpoint still works from a plain fetch.
  const incoming = body.message ?? body.messages?.at(-1);
  if (!incoming) {
    return new Response("No message provided.", { status: 400 });
  }

  const firstText = incoming.parts?.find((part) => part.type === "text");
  const conversation = await ensureConversation(
    user.id,
    body.id,
    firstText && "text" in firstText
      ? firstText.text.slice(0, 80)
      : undefined,
  );

  // Persisted parts come back as jsonb. They are trusted because this server
  // wrote them from its own stream, so they are re-typed rather than revalidated.
  const history = (await loadMessages(
    user.id,
    conversation.id,
  )) as ForayUIMessage[];
  const uiMessages = [...history, incoming];

  await saveMessages(user.id, conversation.id, [incoming]);

  // Everything the agent does downstream — including model calls made from
  // inside the SDK's own loop — is attributed to this user by the ledger
  // middleware, which reads the context back out of async-local storage.
  // Built before entering the context so the agent's own profile read is not
  // attributed as a model call.
  const agent = await chatAgent(user.id);

  return withLlmContext({ userId: user.id }, () =>
    createAgentUIStreamResponse({
      agent,
      uiMessages,
      originalMessages: uiMessages,
      headers: { "x-conversation-id": conversation.id },
      // Persist once the stream is done, so a reload shows what the user saw.
      onEnd: async ({ responseMessage }) => {
        if (responseMessage) {
          await saveMessages(user.id, conversation.id, [responseMessage]);
        }
      },
    }),
  );
}
