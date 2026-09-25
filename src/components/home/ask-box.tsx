"use client";

import { ArrowUp } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState, type FormEvent, type KeyboardEvent } from "react";

import { cn } from "@/lib/utils";

/** Where a prompt typed outside the chat waits for the chat to pick it up. */
export const PENDING_PROMPT_KEY = "foray:pending-prompt";

/**
 * Hands a prompt to a new chat. It travels through sessionStorage rather than
 * the URL, so what someone asked never lands in their browser history.
 */
export function startChatWith(router: ReturnType<typeof useRouter>, prompt: string) {
  try {
    sessionStorage.setItem(PENDING_PROMPT_KEY, prompt);
  } catch {
    // Storage blocked: the chat opens empty and the user retypes.
  }
  router.push("/chat");
}

export function AskBox({
  suggestions,
  placeholder = "What should Foray look for?",
  className,
}: {
  suggestions: string[];
  placeholder?: string;
  className?: string;
}) {
  const router = useRouter();
  const [text, setText] = useState("");

  function submit(event?: FormEvent) {
    event?.preventDefault();
    const prompt = text.trim();
    if (!prompt) return;
    startChatWith(router, prompt);
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  }

  return (
    <div className={cn("space-y-3", className)}>
      <form
        className="bg-background focus-within:border-primary/50 focus-within:ring-primary/10 flex items-end gap-2 rounded-2xl border p-2 pl-4 shadow-[0_1px_2px_rgb(23_34_29/0.05)] transition-shadow focus-within:ring-4"
        onSubmit={submit}
      >
        <textarea
          aria-label="Ask Foray"
          className="placeholder:text-muted-foreground/80 max-h-40 min-h-[44px] flex-1 resize-none bg-transparent py-2.5 text-[15px] leading-relaxed outline-none [field-sizing:content]"
          onChange={(event) => setText(event.target.value)}
          onKeyDown={onKeyDown}
          placeholder={placeholder}
          rows={1}
          value={text}
        />
        <button
          aria-label="Send"
          className="bg-primary text-primary-foreground inline-flex size-9 shrink-0 items-center justify-center rounded-xl transition-opacity disabled:opacity-30"
          disabled={!text.trim()}
          type="submit"
        >
          <ArrowUp className="size-4.5" />
        </button>
      </form>
      {suggestions.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {suggestions.map((suggestion) => (
            <button
              className="text-foreground/80 hover:text-foreground hover:border-primary/40 hover:bg-accent rounded-full border px-3 py-1.5 text-[13px] transition-colors"
              key={suggestion}
              onClick={() => startChatWith(router, suggestion)}
              type="button"
            >
              {suggestion}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
