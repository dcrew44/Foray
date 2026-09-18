"use client";

import { useChat } from "@ai-sdk/react";
import { DefaultChatTransport, isStaticToolUIPart } from "ai";
import { useRouter } from "next/navigation";
import { useState } from "react";

import {
  Conversation,
  ConversationContent,
  ConversationEmptyState,
  ConversationScrollButton,
} from "@/components/ai-elements/conversation";
import {
  Message,
  MessageContent,
  MessageResponse,
} from "@/components/ai-elements/message";
import {
  PromptInput,
  PromptInputBody,
  PromptInputSubmit,
  PromptInputTextarea,
} from "@/components/ai-elements/prompt-input";
import {
  Tool,
  ToolContent,
  ToolHeader,
  ToolInput,
  ToolOutput,
} from "@/components/ai-elements/tool";
import type { ForayUIMessage } from "@/lib/agents/chat-agent";

export function Chat({
  conversationId,
  initialMessages,
}: {
  conversationId: string | null;
  initialMessages: ForayUIMessage[];
}) {
  const router = useRouter();
  const [id] = useState(() => conversationId ?? crypto.randomUUID());

  const { messages, sendMessage, status, error } = useChat<ForayUIMessage>({
    id,
    messages: initialMessages,
    transport: new DefaultChatTransport({
      api: "/api/chat",
      // Only the newest message goes over the wire; Postgres holds the history
      // and is the authority on what was said.
      prepareSendMessagesRequest: ({ id: chatId, messages: all }) => ({
        body: { id: chatId, message: all.at(-1) },
      }),
    }),
    onFinish: () => {
      // Put the conversation in the URL so a reload lands back on this thread.
      if (!conversationId) router.replace(`/chat/${id}`);
      router.refresh();
    },
  });

  return (
    <div className="flex h-svh flex-col">
      <Conversation className="flex-1">
        <ConversationContent>
          {messages.length === 0 && (
            <ConversationEmptyState
              description="Ask about your search, or tell me what you are looking for."
              title="Foray"
            />
          )}

          {messages.map((message) => (
            <Message from={message.role} key={message.id}>
              <MessageContent>
                {message.parts.map((part, index) => {
                  if (part.type === "text") {
                    return (
                      <MessageResponse key={`${message.id}-${index}`}>
                        {part.text}
                      </MessageResponse>
                    );
                  }

                  // Static tools only — the agent declares its tool set, so a
                  // dynamic-tool part would mean something went wrong upstream.
                  if (isStaticToolUIPart(part)) {
                    return (
                      <Tool key={`${message.id}-${index}`}>
                        <ToolHeader state={part.state} type={part.type} />
                        <ToolContent>
                          <ToolInput input={part.input} />
                          <ToolOutput
                            errorText={part.errorText}
                            output={part.output}
                          />
                        </ToolContent>
                      </Tool>
                    );
                  }

                  return null;
                })}
              </MessageContent>
            </Message>
          ))}
        </ConversationContent>
        <ConversationScrollButton />
      </Conversation>

      {error && (
        <p className="text-destructive px-4 pb-2 text-sm" role="alert">
          {error.message}
        </p>
      )}

      <div className="border-border border-t p-4">
        <PromptInput
          onSubmit={(message) => {
            if (!message.text.trim()) return;
            void sendMessage({ text: message.text });
          }}
        >
          <PromptInputBody>
            <PromptInputTextarea placeholder="Ask anything about your search…" />
          </PromptInputBody>
          <PromptInputSubmit status={status} />
        </PromptInput>
      </div>
    </div>
  );
}
