"use client";

import type { ReactNode } from "react";

import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

/** A labelled input. Kept tiny so the section editors stay readable. */
export function Field({
  label,
  value,
  onChange,
  placeholder,
  className,
}: {
  label: string;
  value: string | null | undefined;
  onChange: (value: string) => void;
  placeholder?: string;
  className?: string;
}) {
  return (
    <label className={`block space-y-1 ${className ?? ""}`}>
      <span className="text-muted-foreground text-xs font-medium">{label}</span>
      <Input
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        value={value ?? ""}
      />
    </label>
  );
}

export function AreaField({
  label,
  value,
  onChange,
  rows = 4,
  hint,
}: {
  label: string;
  value: string | null | undefined;
  onChange: (value: string) => void;
  rows?: number;
  hint?: string;
}) {
  return (
    <label className="block space-y-1">
      <span className="text-muted-foreground text-xs font-medium">
        {label}
        {hint && <span className="ml-2 font-normal opacity-70">{hint}</span>}
      </span>
      <Textarea
        onChange={(event) => onChange(event.target.value)}
        rows={rows}
        value={value ?? ""}
      />
    </label>
  );
}

/**
 * Shows where an item came from. Provenance is only meaningful if the user can
 * see it — otherwise "never overwrite the user's edits" is an invisible promise.
 */
export function SourceBadge({ source }: { source: string }) {
  const label =
    source === "user"
      ? "edited by you"
      : source === "linkedin_pdf"
        ? "from LinkedIn"
        : "from your resume";

  return (
    <span className="text-muted-foreground rounded border px-1.5 py-0.5 text-[10px] uppercase tracking-wide">
      {label}
    </span>
  );
}

export function SectionCard({
  children,
  actions,
}: {
  children: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="border-border space-y-3 rounded-lg border p-4">
      {actions && <div className="flex justify-end gap-2">{actions}</div>}
      {children}
    </div>
  );
}
