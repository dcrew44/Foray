"use client";

import { FilePen, FileText, IdCard, Sparkles } from "lucide-react";
import { useId, type ComponentProps } from "react";

import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

/**
 * A labelled input. Kept tiny so the section editors stay readable.
 *
 * Optional resume fields are stored as null rather than "", so an emptied
 * field reads the same as one that was never filled — and a cleared date does
 * not fail the schema's non-empty rule on save.
 */
export function Field({
  label,
  value,
  onChange,
  placeholder,
  className,
  optional = true,
  ...props
}: {
  label: string;
  value: string | null | undefined;
  onChange: (value: string | null) => void;
  placeholder?: string;
  className?: string;
  /** Empty becomes null. Turn off for fields that are always a string. */
  optional?: boolean;
} & Omit<ComponentProps<"input">, "value" | "onChange" | "className">) {
  const id = useId();
  return (
    <div className={cn("min-w-0 space-y-1.5", className)}>
      <label className="text-muted-foreground block text-[12.5px]" htmlFor={id}>
        {label}
      </label>
      <Input
        className="h-9 text-[13.5px] md:text-[13.5px]"
        id={id}
        onChange={(event) => {
          const next = event.target.value;
          onChange(optional && next.trim() === "" ? null : next);
        }}
        placeholder={placeholder}
        value={value ?? ""}
        {...props}
      />
    </div>
  );
}

export function AreaField({
  label,
  value,
  onChange,
  placeholder,
  hint,
  className,
}: {
  label: string;
  value: string | null | undefined;
  onChange: (value: string | null) => void;
  placeholder?: string;
  hint?: string;
  className?: string;
}) {
  const id = useId();
  return (
    <div className={cn("min-w-0 space-y-1.5", className)}>
      <label className="text-muted-foreground block text-[12.5px]" htmlFor={id}>
        {label}
        {hint && <span className="text-muted-foreground/80 ml-2">{hint}</span>}
      </label>
      <textarea
        className={cn(
          "border-input placeholder:text-muted-foreground dark:bg-input/30 field-sizing-content min-h-[4.5rem] w-full rounded-lg border bg-transparent px-2.5 py-2 text-[13.5px] leading-relaxed transition-colors outline-none",
          "focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-3",
        )}
        id={id}
        onChange={(event) => {
          const next = event.target.value;
          onChange(next.trim() === "" ? null : next);
        }}
        placeholder={placeholder}
        value={value ?? ""}
      />
    </div>
  );
}

/**
 * Where an item came from. Provenance is only meaningful if the user can see
 * it — otherwise "never overwrite the user's edits" is an invisible promise.
 */
export function SourceNote({
  source,
  isNew = false,
  className,
}: {
  source: string;
  /** Added in this editing session and not saved yet. */
  isNew?: boolean;
  className?: string;
}) {
  const { label, Icon } = isNew
    ? { label: "Added by you", Icon: Sparkles }
    : source === "user"
      ? { label: "Edited by you", Icon: FilePen }
      : source === "linkedin_pdf"
        ? { label: "From LinkedIn", Icon: IdCard }
        : { label: "From your resume", Icon: FileText };

  return (
    <span
      className={cn(
        "text-muted-foreground inline-flex items-center gap-1.5 text-[12.5px]",
        className,
      )}
    >
      <Icon className="size-3.5 opacity-70" />
      {label}
    </span>
  );
}
