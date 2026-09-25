"use client";

import { Radio } from "@base-ui/react/radio";
import { RadioGroup } from "@base-ui/react/radio-group";
import { X } from "lucide-react";
import {
  useEffect,
  useRef,
  useState,
  type ClipboardEvent,
  type KeyboardEvent,
  type ReactNode,
} from "react";

import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/utils";

/**
 * The small form kit the profile and settings pages share: a labelled row, a
 * tag input, a segmented choice and the bar that saves or discards an edit.
 * Every form here edits a local copy and saves it in one go, so these are all
 * controlled and hold no data of their own.
 */

/**
 * One row of a settings-style form: what the field is and why it matters on
 * the left, the control on the right. Rows stack inside one bordered
 * container with hairlines between them.
 */
export function FormRow({
  id,
  label,
  hint,
  note,
  children,
  className,
}: {
  /** The control's id. The label points at it; group controls use `${id}-label`. */
  id: string;
  label: ReactNode;
  hint?: ReactNode;
  /** A quiet status line under the hint, e.g. "Needed to search". */
  note?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "grid gap-x-8 gap-y-3 px-5 py-4 sm:grid-cols-[minmax(0,14rem)_minmax(0,1fr)]",
        className,
      )}
    >
      <div className="min-w-0 space-y-1">
        <label className="block text-[13.5px] font-medium" htmlFor={id} id={`${id}-label`}>
          {label}
        </label>
        {hint && (
          <p className="text-muted-foreground text-[12.5px] leading-snug">{hint}</p>
        )}
        {note}
      </div>
      <div className="min-w-0 self-center">{children}</div>
    </div>
  );
}

/**
 * Free-form values as removable chips: type and press Enter to add one.
 * Commas are allowed inside a value — "San Francisco, CA" is one location —
 * so only Enter, leaving the field, or pasting several lines adds chips.
 */
export function TagInput({
  id,
  value,
  onChange,
  placeholder,
  labelledBy,
  disabled,
  className,
}: {
  id?: string;
  value: string[];
  onChange: (next: string[]) => void;
  placeholder?: string;
  labelledBy?: string;
  disabled?: boolean;
  className?: string;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [draft, setDraft] = useState("");

  function add(raw: string) {
    const parts = raw
      .split(/\n+/)
      .map((part) => part.trim())
      .filter(Boolean);
    if (parts.length === 0) return;

    const seen = new Set(value.map((item) => item.toLowerCase()));
    const next = [...value];
    for (const part of parts) {
      if (seen.has(part.toLowerCase())) continue;
      seen.add(part.toLowerCase());
      next.push(part);
    }
    if (next.length !== value.length) onChange(next);
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter") {
      event.preventDefault();
      add(draft);
      setDraft("");
    } else if (event.key === "Backspace" && draft === "" && value.length > 0) {
      onChange(value.slice(0, -1));
    }
  }

  function onPaste(event: ClipboardEvent<HTMLInputElement>) {
    const text = event.clipboardData.getData("text");
    if (!text.includes("\n")) return;
    event.preventDefault();
    add(text);
    setDraft("");
  }

  return (
    <div
      className={cn(
        "border-input dark:bg-input/30 flex min-h-9 w-full cursor-text flex-wrap items-center gap-1.5 rounded-lg border bg-transparent p-1.5 transition-colors",
        "focus-within:border-ring focus-within:ring-ring/50 focus-within:ring-3",
        disabled && "pointer-events-none opacity-50",
        className,
      )}
      onClick={(event) => {
        if (event.target === event.currentTarget) input.current?.focus();
      }}
    >
      {value.map((tag, index) => (
        <span
          className="bg-secondary text-secondary-foreground inline-flex h-6 max-w-full items-center gap-0.5 rounded-md pr-0.5 pl-2 text-[13px]"
          key={`${tag}-${index}`}
        >
          <span className="truncate">{tag}</span>
          <button
            aria-label={`Remove ${tag}`}
            className="text-muted-foreground hover:text-foreground hover:bg-foreground/10 focus-visible:ring-ring/60 inline-flex size-5 shrink-0 items-center justify-center rounded outline-none focus-visible:ring-2"
            onClick={() => onChange(value.filter((_, i) => i !== index))}
            type="button"
          >
            <X className="size-3" />
          </button>
        </span>
      ))}
      <input
        aria-labelledby={labelledBy}
        className="placeholder:text-muted-foreground h-6 min-w-[12ch] flex-1 bg-transparent px-1.5 text-[13.5px] outline-none"
        disabled={disabled}
        id={id}
        onBlur={(event) => {
          // Focus moving to a chip's remove button is not "done typing": adding
          // the draft there would race the removal and drop one of the two.
          const next = event.relatedTarget;
          if (next && event.currentTarget.parentElement?.contains(next)) return;
          add(draft);
          setDraft("");
        }}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={onKeyDown}
        onPaste={onPaste}
        placeholder={value.length === 0 ? placeholder : "Add another"}
        ref={input}
        value={draft}
      />
    </div>
  );
}

export type SegmentOption<T extends string> = { value: T; label: ReactNode };

/**
 * One choice from a handful, all visible at once. Radio semantics underneath,
 * so arrow keys move between options.
 */
export function Segmented<T extends string>({
  value,
  onChange,
  options,
  labelledBy,
  ariaLabel,
  disabled,
  className,
}: {
  value: T | null;
  onChange: (next: T) => void;
  options: Array<SegmentOption<T>>;
  labelledBy?: string;
  ariaLabel?: string;
  disabled?: boolean;
  className?: string;
}) {
  return (
    <RadioGroup
      aria-label={ariaLabel}
      aria-labelledby={labelledBy}
      className={cn(
        "bg-muted inline-flex max-w-full flex-wrap gap-0.5 rounded-lg p-0.5",
        className,
      )}
      disabled={disabled}
      onValueChange={(next) => onChange(next as T)}
      value={value}
    >
      {options.map((option) => (
        <Radio.Root
          className={cn(
            "text-muted-foreground inline-flex h-7 items-center justify-center rounded-md px-3 text-[13px] font-medium whitespace-nowrap transition-colors outline-none select-none",
            "hover:text-foreground focus-visible:ring-ring/50 focus-visible:ring-3",
            "data-checked:bg-background data-checked:text-foreground data-checked:ring-border data-checked:shadow-[0_1px_2px_rgb(23_34_29/0.08)] data-checked:ring-1",
            "data-disabled:opacity-50",
          )}
          key={option.value}
          value={option.value}
        >
          {option.label}
        </Radio.Root>
      ))}
    </RadioGroup>
  );
}

/**
 * The bar that appears once a form has unsaved changes. It sticks to the
 * bottom of the window while its form is on screen, so saving never means
 * scrolling back to find a button.
 */
export function SaveBar({
  visible,
  pending,
  onSave,
  onDiscard,
  message = "You have unsaved changes.",
  saveLabel = "Save changes",
  sticky = true,
  className,
}: {
  visible: boolean;
  pending: boolean;
  onSave: () => void;
  onDiscard: () => void;
  message?: ReactNode;
  saveLabel?: string;
  sticky?: boolean;
  className?: string;
}) {
  if (!visible) return null;

  return (
    <div
      aria-label="Unsaved changes"
      className={cn(
        "bg-popover text-popover-foreground animate-in fade-in slide-in-from-bottom-1 flex items-center gap-2 rounded-xl border py-2 pr-2 pl-4 duration-150",
        sticky && "sticky bottom-4 z-20 shadow-[0_8px_24px_-8px_rgb(23_34_29/0.25)]",
        className,
      )}
      role="region"
    >
      <p className="min-w-0 flex-1 truncate text-[13.5px]">
        <span className="sm:hidden">Unsaved changes</span>
        <span className="hidden sm:inline">{message}</span>
      </p>
      <Button disabled={pending} onClick={onDiscard} type="button" variant="ghost">
        Discard
      </Button>
      <Button disabled={pending} onClick={onSave} type="button">
        {pending && <Spinner />}
        {pending ? "Saving…" : saveLabel}
      </Button>
    </div>
  );
}

/**
 * While a form is dirty: warn before the tab closes, and let Cmd/Ctrl+S save.
 * Both only while there is something to lose.
 */
export function useUnsavedChanges(dirty: boolean, save: () => void) {
  const saveRef = useRef(save);
  useEffect(() => {
    saveRef.current = save;
  });

  useEffect(() => {
    if (!dirty) return;
    const beforeUnload = (event: BeforeUnloadEvent) => event.preventDefault();
    const keyDown = (event: globalThis.KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        saveRef.current();
      }
    };
    window.addEventListener("beforeunload", beforeUnload);
    window.addEventListener("keydown", keyDown);
    return () => {
      window.removeEventListener("beforeunload", beforeUnload);
      window.removeEventListener("keydown", keyDown);
    };
  }, [dirty]);
}

/**
 * Content equality for dirty checks: key order, and the difference between a
 * missing key, `undefined` and `null`, do not count as edits.
 */
export function sameContent(a: unknown, b: unknown): boolean {
  return stable(a) === stable(b);
}

function stable(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.entries(value as Record<string, unknown>)
      .filter(([, v]) => v !== undefined && v !== null)
      .sort(([x], [y]) => (x < y ? -1 : x > y ? 1 : 0))
      .map(([k, v]) => `${JSON.stringify(k)}:${stable(v)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value ?? null);
}
