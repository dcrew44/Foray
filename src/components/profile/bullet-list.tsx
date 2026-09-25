"use client";

import { Plus, X } from "lucide-react";
import { useEffect, useId, useRef, type ClipboardEvent, type KeyboardEvent } from "react";

import { cn } from "@/lib/utils";

/**
 * Resume bullets, edited as bullets.
 *
 * Enter starts a new bullet at the caret, Backspace at the start of one joins
 * it to the one above, and pasting several lines makes several bullets — so
 * it behaves like the list it is rather than a textarea with a rule about
 * line breaks. Empty bullets are allowed while editing; the editor drops them
 * when it saves.
 */
export function BulletList({
  label,
  value,
  onChange,
  placeholder = "What you did, and what came of it",
  addLabel = "Add a bullet",
}: {
  label: string;
  value: string[];
  onChange: (next: string[]) => void;
  placeholder?: string;
  addLabel?: string;
}) {
  const labelId = useId();
  const fields = useRef<Array<HTMLTextAreaElement | null>>([]);
  const pendingFocus = useRef<{ index: number; caret: number } | null>(null);

  // Focus follows the edit that just re-rendered the list: the new bullet
  // after Enter, the joined one after Backspace.
  useEffect(() => {
    const target = pendingFocus.current;
    if (!target) return;
    pendingFocus.current = null;
    focusAt(target.index, target.caret);
  });

  function focusAt(index: number, caret: number) {
    const field = fields.current[index];
    if (!field) return;
    field.focus();
    field.setSelectionRange(caret, caret);
  }

  function replace(start: number, count: number, items: string[], focus?: { index: number; caret: number }) {
    const next = [...value];
    next.splice(start, count, ...items);
    if (focus) pendingFocus.current = focus;
    onChange(next);
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>, index: number) {
    const field = event.currentTarget;
    const start = field.selectionStart;
    const end = field.selectionEnd;
    const text = value[index] ?? "";

    if (event.key === "Enter" && !event.nativeEvent.isComposing) {
      event.preventDefault();
      replace(index, 1, [text.slice(0, start), text.slice(end)], { index: index + 1, caret: 0 });
    } else if (event.key === "Backspace" && start === 0 && end === 0 && index > 0) {
      event.preventDefault();
      const previous = value[index - 1] ?? "";
      replace(index - 1, 2, [previous + text], { index: index - 1, caret: previous.length });
    } else if (event.key === "ArrowUp" && start === 0 && end === 0 && index > 0) {
      event.preventDefault();
      focusAt(index - 1, (value[index - 1] ?? "").length);
    } else if (
      event.key === "ArrowDown" &&
      start === text.length &&
      end === text.length &&
      index < value.length - 1
    ) {
      event.preventDefault();
      focusAt(index + 1, 0);
    }
  }

  function onPaste(event: ClipboardEvent<HTMLTextAreaElement>, index: number) {
    const pasted = event.clipboardData.getData("text");
    if (!/\r?\n/.test(pasted)) return;
    event.preventDefault();

    // Pasted from a document, lines usually still carry their bullet marks.
    const lines = pasted
      .split(/\r?\n/)
      .map((line) => line.replace(/^\s*(?:[-•*▪◦·]|\d+[.)])\s+/, "").trim())
      .filter(Boolean);
    if (lines.length === 0) return;

    const field = event.currentTarget;
    const text = value[index] ?? "";
    const before = text.slice(0, field.selectionStart);
    const after = text.slice(field.selectionEnd);
    const pieces = [...lines];
    const last = pieces.length - 1;
    pieces[0] = before + pieces[0];
    const caret = pieces[last]!.length;
    pieces[last] = pieces[last] + after;

    replace(index, 1, pieces, { index: index + last, caret });
  }

  return (
    <div className="min-w-0 space-y-1.5">
      <p className="text-muted-foreground text-[12.5px]" id={labelId}>
        {label}
      </p>
      <div
        aria-labelledby={labelId}
        className="border-input dark:bg-input/30 focus-within:border-ring focus-within:ring-ring/50 rounded-lg border py-1 transition-colors focus-within:ring-3"
        role="group"
      >
        {value.length > 0 && (
          <ul>
            {value.map((bullet, index) => (
              <li className="group/bullet flex items-start gap-2.5 pr-1 pl-3" key={index}>
                <span
                  aria-hidden
                  className="bg-muted-foreground/60 mt-[13px] size-1 shrink-0 rounded-full"
                />
                <textarea
                  aria-label={`${label}, bullet ${index + 1}`}
                  className="placeholder:text-muted-foreground/70 field-sizing-content min-h-[30px] flex-1 resize-none bg-transparent py-1 text-[13.5px] leading-relaxed outline-none"
                  onChange={(event) =>
                    replace(index, 1, [event.target.value.replace(/\r?\n/g, " ")])
                  }
                  onKeyDown={(event) => onKeyDown(event, index)}
                  onPaste={(event) => onPaste(event, index)}
                  placeholder={placeholder}
                  ref={(element) => {
                    fields.current[index] = element;
                  }}
                  rows={1}
                  value={bullet}
                />
                <button
                  aria-label={`Remove bullet ${index + 1}`}
                  className={cn(
                    "text-muted-foreground hover:text-foreground hover:bg-muted focus-visible:ring-ring/50 mt-1 inline-flex size-6 shrink-0 items-center justify-center rounded-md outline-none focus-visible:ring-2",
                    "sm:opacity-0 sm:group-focus-within/bullet:opacity-100 sm:group-hover/bullet:opacity-100 sm:focus-visible:opacity-100",
                  )}
                  onClick={() => replace(index, 1, [])}
                  type="button"
                >
                  <X className="size-3.5" />
                </button>
              </li>
            ))}
          </ul>
        )}
        <button
          className="text-muted-foreground hover:text-foreground focus-visible:ring-ring/50 mx-1 inline-flex h-7 items-center gap-1.5 rounded-md px-2 text-[12.5px] outline-none focus-visible:ring-2"
          onClick={() => replace(value.length, 0, [""], { index: value.length, caret: 0 })}
          type="button"
        >
          <Plus className="size-3.5" />
          {addLabel}
        </button>
      </div>
    </div>
  );
}
