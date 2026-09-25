"use client";

import { useChat } from "@ai-sdk/react";
import {
  DefaultChatTransport,
  getStaticToolName,
  isStaticToolUIPart,
  isTextUIPart,
} from "ai";
import { ArrowUp, CircleAlert, MessageSquarePlus, RotateCcw, Square } from "lucide-react";
import Link from "next/link";
import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";

import {
  Conversation,
  ConversationContent,
  ConversationScrollButton,
} from "@/components/ai-elements/conversation";
import { MessageResponse } from "@/components/ai-elements/message";
import { Blaze } from "@/components/foray/primitives";
import { PENDING_PROMPT_KEY } from "@/components/home/ask-box";
import { requestLiveRefresh } from "@/components/live/live-provider";
import { announceConversationsChanged } from "@/components/shell/recent-chats";
import { buttonVariants } from "@/components/ui/button";
import type { ForayUIMessage } from "@/lib/agents/chat-agent";
import { cn } from "@/lib/utils";

import { ToolPart, type ToolPartView } from "./tool-part";

/**
 * The conversation with the agent.
 *
 * Postgres holds the history and is the authority, so only the newest message
 * goes over the wire. Tool calls render as what they did — a live run card, a
 * list of matches, a sentence — rather than as JSON.
 */

/** A starting point. `prefill` ones need the user to finish the sentence. */
export type ChatSuggestion = { title: string; prompt: string; prefill?: boolean };

export function Chat({
  chatId,
  isNew,
  initialMessages,
  title,
  suggestions,
}: {
  chatId: string;
  isNew: boolean;
  initialMessages: ForayUIMessage[];
  title: string | null;
  suggestions: ChatSuggestion[];
}) {
  const [persisted, setPersisted] = useState(!isNew);
  const [text, setText] = useState("");
  const textarea = useRef<HTMLTextAreaElement>(null);

  const { messages, sendMessage, status, error, stop, regenerate, clearError } =
    useChat<ForayUIMessage>({
      id: chatId,
      messages: initialMessages,
      transport: new DefaultChatTransport({
        api: "/api/chat",
        prepareSendMessagesRequest: ({ id, messages: all }) => ({
          body: { id, message: all.at(-1) },
        }),
      }),
      onFinish: () => {
        // Tools may have started runs; pull their state now rather than on
        // the next tick.
        requestLiveRefresh();
        if (!persisted) {
          // Address bar only: a real navigation would remount this component
          // and drop the reply that just streamed in.
          window.history.replaceState(null, "", `/chat/${chatId}`);
          setPersisted(true);
          announceConversationsChanged();
        }
      },
    });

  const busy = status === "submitted" || status === "streaming";

  function send(prompt: string) {
    const trimmed = prompt.trim();
    if (!trimmed || busy) return;
    clearError();
    void sendMessage({ text: trimmed });
    setText("");
  }

  // A prompt typed on Home arrives here. The hand-off waits a tick and is
  // cancelled on cleanup: a mount that is immediately unmounted again (React
  // does this deliberately in development) would otherwise consume the prompt
  // and then abort the request it started, and the message would vanish.
  const handedOff = useRef(false);
  useEffect(() => {
    if (!isNew || handedOff.current) return;
    const timer = setTimeout(() => {
      handedOff.current = true;
      let pending: string | null = null;
      try {
        pending = sessionStorage.getItem(PENDING_PROMPT_KEY);
        sessionStorage.removeItem(PENDING_PROMPT_KEY);
      } catch {
        // Storage blocked: nothing was handed over.
      }
      if (pending) {
        void sendMessage({ text: pending });
      } else {
        textarea.current?.focus();
      }
    }, 0);
    return () => clearTimeout(timer);
  }, [isNew, sendMessage]);

  const last = messages.at(-1);
  const thinking =
    status === "submitted" ||
    (status === "streaming" &&
      last?.role === "assistant" &&
      !last.parts.some((part) => isTextUIPart(part) || isStaticToolUIPart(part)));

  return (
    <div className="flex h-[calc(100svh-3.5rem)] flex-col lg:h-[calc(100svh-1rem-2px)]">
      <header className="flex h-13 shrink-0 items-center gap-3 border-b px-5">
        <h1 className="min-w-0 flex-1 truncate text-[14px] font-medium">
          {title ?? (messages.length ? firstUserText(messages) : "New chat")}
        </h1>
        {messages.length > 0 && (
          <Link className={buttonVariants({ size: "sm", variant: "ghost" })} href="/chat">
            <MessageSquarePlus />
            New chat
          </Link>
        )}
      </header>

      {messages.length === 0 ? (
        <EmptyChat
          onPick={(suggestion) => {
            if (suggestion.prefill) {
              setText(suggestion.prompt);
              textarea.current?.focus();
            } else {
              send(suggestion.prompt);
            }
          }}
          suggestions={suggestions}
        />
      ) : (
        <Conversation className="min-h-0 flex-1">
          <ConversationContent className="mx-auto w-full max-w-[760px] gap-7 px-5 pt-8 pb-10">
            {messages.map((message) =>
              message.role === "user" ? (
                <UserMessage key={message.id} message={message} />
              ) : (
                <AssistantMessage key={message.id} message={message} />
              ),
            )}

            {thinking && (
              <div className="text-muted-foreground flex items-center gap-2.5 text-[13.5px]">
                <Blaze className="h-3.5 w-2 rounded-[2px]" live />
                <span className="animate-pulse">Thinking…</span>
              </div>
            )}

            {error && (
              <div
                className="border-destructive/30 bg-destructive/5 flex items-start gap-3 rounded-xl border px-4 py-3 text-[13.5px]"
                role="alert"
              >
                <CircleAlert className="text-destructive mt-0.5 size-4 shrink-0" />
                <div className="min-w-0 flex-1">
                  <p className="font-medium">That reply did not come through.</p>
                  <p className="text-muted-foreground mt-0.5">
                    {error.message || "The model or the network failed mid-answer."}
                  </p>
                </div>
                <button
                  className={buttonVariants({ size: "sm", variant: "outline" })}
                  onClick={() => {
                    clearError();
                    void regenerate();
                  }}
                  type="button"
                >
                  <RotateCcw />
                  Try again
                </button>
              </div>
            )}
          </ConversationContent>
          <ConversationScrollButton />
        </Conversation>
      )}

      <Composer
        busy={busy}
        onStop={stop}
        onSubmit={() => send(text)}
        ref={textarea}
        setText={setText}
        text={text}
      />
    </div>
  );
}

function firstUserText(messages: ForayUIMessage[]): string {
  const first = messages.find((message) => message.role === "user");
  const part = first?.parts.find(isTextUIPart);
  return part?.text.slice(0, 80) ?? "New chat";
}

function UserMessage({ message }: { message: ForayUIMessage }) {
  const text = message.parts
    .filter(isTextUIPart)
    .map((part) => part.text)
    .join("\n");
  return (
    <div className="flex justify-end">
      <div className="bg-accent text-foreground max-w-[85%] rounded-2xl rounded-br-md px-4 py-2.5 text-[15px] leading-relaxed whitespace-pre-wrap">
        {text}
      </div>
    </div>
  );
}

function AssistantMessage({ message }: { message: ForayUIMessage }) {
  return (
    <div className="flex min-w-0 flex-col gap-3">
      {message.parts.map((part, index) => {
        const key = `${message.id}-${index}`;
        if (isTextUIPart(part)) {
          if (!part.text.trim()) return null;
          return (
            <MessageResponse className="prose-foray text-foreground" key={key}>
              {part.text}
            </MessageResponse>
          );
        }
        if (isStaticToolUIPart(part)) {
          const view: ToolPartView = {
            name: getStaticToolName(part),
            state: part.state as ToolPartView["state"],
            input: "input" in part ? part.input : undefined,
            output: "output" in part ? part.output : undefined,
            errorText: "errorText" in part ? (part.errorText as string | undefined) : undefined,
          };
          return <ToolPart key={key} part={view} />;
        }
        return null;
      })}
    </div>
  );
}

function EmptyChat({
  suggestions,
  onPick,
}: {
  suggestions: ChatSuggestion[];
  onPick: (suggestion: ChatSuggestion) => void;
}) {
  return (
    <div className="flex min-h-0 flex-1 flex-col justify-center overflow-y-auto px-5 py-10">
      <div className="mx-auto w-full max-w-[680px]">
        <p className="text-display text-[30px] leading-tight sm:text-[36px]">
          What should Foray do?
        </p>
        <p className="text-muted-foreground mt-2 max-w-[56ch] text-[15px]">
          Ask for roles in your own words, have it explain why a match fits, move jobs
          on your board, or tailor your resume to a posting.
        </p>
        {suggestions.length > 0 && (
          <div className="mt-8 grid gap-2 sm:grid-cols-2">
            {suggestions.map((suggestion) => (
              <button
                className="hover:border-primary/40 hover:bg-accent/60 rounded-xl border px-4 py-3 text-left transition-colors"
                key={suggestion.title}
                onClick={() => onPick(suggestion)}
                type="button"
              >
                <span className="block text-[14px] font-medium">{suggestion.title}</span>
                <span className="text-muted-foreground mt-0.5 block text-[12.5px] leading-snug">
                  {suggestion.prompt}
                </span>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function Composer({
  text,
  setText,
  busy,
  onSubmit,
  onStop,
  ref,
}: {
  text: string;
  setText: (value: string) => void;
  busy: boolean;
  onSubmit: () => void;
  onStop: () => void;
  ref: React.Ref<HTMLTextAreaElement>;
}) {
  function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) onStop();
    else onSubmit();
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      if (!busy) onSubmit();
    }
  }

  return (
    <div className="shrink-0 px-4 pb-4 sm:px-5">
      <form
        className="bg-background focus-within:border-primary/50 focus-within:ring-primary/10 mx-auto flex w-full max-w-[760px] items-end gap-2 rounded-2xl border p-2 pl-4 shadow-[0_2px_8px_-2px_rgb(23_34_29/0.08)] transition-shadow focus-within:ring-4"
        onSubmit={submit}
      >
        <textarea
          aria-label="Message Foray"
          className="placeholder:text-muted-foreground/80 max-h-48 min-h-[44px] flex-1 resize-none bg-transparent py-2.5 text-[15px] leading-relaxed outline-none [field-sizing:content]"
          onChange={(event) => setText(event.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Ask Foray anything about your search…"
          ref={ref}
          rows={1}
          value={text}
        />
        <button
          aria-label={busy ? "Stop" : "Send"}
          className={cn(
            "inline-flex size-9 shrink-0 items-center justify-center rounded-xl transition-opacity",
            busy
              ? "bg-foreground text-background"
              : "bg-primary text-primary-foreground disabled:opacity-30",
          )}
          disabled={!busy && !text.trim()}
          type="submit"
        >
          {busy ? <Square className="size-3.5 fill-current" /> : <ArrowUp className="size-4.5" />}
        </button>
      </form>
      <p className="text-muted-foreground mx-auto mt-2 max-w-[760px] text-center text-[11.5px]">
        Foray works from your saved resume and preferences. It never applies or emails anyone
        without asking.
      </p>
    </div>
  );
}
