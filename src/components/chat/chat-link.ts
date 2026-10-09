import { DefaultChatTransport } from "ai";

import type { ForayUIMessage } from "@/lib/api/chat-types";

/**
 * How one chat talks to Django's `POST /api/chat` (design §7.3), and whether
 * Django has claimed its id yet.
 *
 * - Only the newest message is sent: Postgres holds the history. The body is
 *   `{id, message, tz}`, where `tz` is the browser's time zone, so the agent's
 *   "Today is …" is the user's own day (Django stores it on the user before
 *   the turn, §7.1).
 * - Django answers a turn it accepted with `X-Conversation-Id`, after the
 *   message is saved and the id claimed. A turn it refused (throttled,
 *   unreadable, someone else's id) wrote nothing and answers plain text,
 *   which `useChat` shows as the error. `claimed()` tells a new chat whether
 *   its `/chat/<id>` address exists yet.
 */
export function chatLink(chatId: string, alreadyClaimed: boolean) {
  let claimed = alreadyClaimed;
  const transport = new DefaultChatTransport<ForayUIMessage>({
    api: "/api/chat",
    prepareSendMessagesRequest: ({ id, messages }) => ({
      body: { id, message: messages.at(-1), tz: browserTimeZone() },
    }),
    fetch: async (input, init) => {
      const response = await fetch(input, init);
      if (response.headers.get("x-conversation-id") === chatId) claimed = true;
      return response;
    },
  });
  return { transport, claimed: () => claimed };
}

/** The browser's IANA time zone, or nothing when it cannot say. */
export function browserTimeZone(): string | undefined {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || undefined;
  } catch {
    return undefined;
  }
}
