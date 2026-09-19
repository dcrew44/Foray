"use client";

import { useChat } from "@ai-sdk/react";
import { DefaultChatTransport, isStaticToolUIPart } from "ai";
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
  PromptInputFooter,
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

import { RunCard } from "./run-card";

export function Chat({
  conversationId,
  initialMessages,
}: {
  conversationId: string | null;
  initialMessages: ForayUIMessage[];
}) {
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
      //
      // Deliberately the native history API rather than `router.replace`: a
      // real navigation to /chat/<id> remounts this component with
      // `initialMessages` re-read from Postgres, which throws away the reply
      // that just streamed in. `replaceState` rewrites the address bar only —
      // no remount, no refetch, so the client keeps its own messages, and a
      // genuine reload still server-renders the thread from the database.
      // Nothing else on this route is server-rendered from the conversation,
      // so there is nothing for `router.refresh()` to bring up to date either.
      if (!conversationId) {
        window.history.replaceState(null, "", `/chat/${id}`);
      }
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
                  // A started run gets a live card rather than a JSON blob:
                  // the run outlives this turn, so its progress is the useful
                  // thing to show.
                  if (
                    part.type === "tool-start_search_run" &&
                    part.state === "output-available" &&
                    part.output &&
                    typeof part.output === "object" &&
                    "runId" in part.output &&
                    typeof part.output.runId === "string"
                  ) {
                    return (
                      <RunCard
                        key={`${message.id}-${index}`}
                        runId={part.output.runId}
                      />
                    );
                  }

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
          <PromptInputFooter className="justify-end">
            <PromptInputSubmit status={status} />
          </PromptInputFooter>
        </PromptInput>
      </div>
    </div>
  );
}
