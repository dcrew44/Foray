import { CircleSlash } from "lucide-react";

import { Section } from "@/components/foray/primitives";
import { splitSignals, type JobSignal } from "@/lib/job-signals";
import { cn } from "@/lib/utils";

/**
 * Reasons to look twice before applying: the posting has closed, an aggregator's apply link
 * leads somewhere that is not the employer, the role keeps being posted again, or it has been
 * open a long time. Renders nothing for an ordinary posting.
 *
 * Only a closed posting is prominent, because it changes whether applying is possible; the
 * rest are plain notes. None of them is an error or work in progress, so neither `destructive`
 * nor `signal` is used here.
 */
export function JobSignals({ signals }: { signals: JobSignal[] }) {
  if (signals.length === 0) return null;
  const { blocking, notes } = splitSignals(signals);
  return (
    <Section title="Before you apply">
      {blocking.map((signal) => (
        <div className="bg-muted rounded-xl border px-4 py-3.5" key={signal.kind}>
          <p className="flex items-center gap-2 text-[14px] font-semibold">
            <CircleSlash aria-hidden className="size-4 shrink-0" />
            {signal.label}
          </p>
          <p className="text-foreground/80 mt-1 text-[13px] leading-snug">{signal.detail}</p>
        </div>
      ))}
      {notes.length > 0 && (
        <ul className="text-[13px]">
          {notes.map((signal) => (
            <li className="border-b py-2.5 last:border-b-0" key={signal.kind}>
              <p className="font-medium">{signal.label}</p>
              {/* A detail can name a host, which has nowhere to wrap. */}
              <p className="text-muted-foreground mt-0.5 leading-snug break-words">
                {signal.detail}
              </p>
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

/** A quiet marker for a match whose posting has closed, for rows and cards. */
export function ClosedMark({ className }: { className?: string }) {
  return (
    <span
      className={cn(
        "text-muted-foreground inline-flex h-5 shrink-0 items-center rounded-full border px-2 text-[11.5px] font-medium whitespace-nowrap",
        className,
      )}
      title="No longer on the employer's board"
    >
      Closed
    </span>
  );
}
