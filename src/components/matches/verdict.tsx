import { Ban } from "lucide-react";

import { stripFactIds } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * What a match leads with beside its verdict (`VerdictPill`): one line of why, and a blocker
 * when there is one. Django derives both for every row and for the match page
 * (`apps/matching/verdict.py`), so the list, the board, Home and the page say the same thing.
 *
 * Server-safe, and spans throughout: these sit inside links and paragraphs.
 */

/** One line cut with an ellipsis, or two. */
const CLAMP = { 1: "block truncate", 2: "line-clamp-2" } as const;

/**
 * What would rule the match out: the review's first disqualifying gap, or triage's first
 * missed hard requirement. Said in words and in ink: the signal colour and brick mean other
 * things here.
 */
export function Blocker({
  text,
  clamp,
  className,
}: {
  text: string | null | undefined;
  /** How many lines it may take before it is cut; uncut when not given. */
  clamp?: 1 | 2;
  className?: string;
}) {
  const blocker = stripFactIds(text);
  if (!blocker) return null;
  return (
    <span className={cn("text-foreground flex min-w-0 items-start gap-1.5", className)}>
      <Ban aria-hidden className="mt-[0.18em] size-[1em] shrink-0 opacity-70" />
      <span className="min-w-0 flex-1">
        <span className={clamp && CLAMP[clamp]} title={clamp && blocker}>
          <span className="font-medium">Blocker:</span> {blocker}
        </span>
      </span>
    </span>
  );
}

/** The reason and any blocker, stacked: what sits under a row's title and verdict. */
export function Why({
  reason,
  blocker,
  clamp = 1,
  className,
}: {
  reason: string | null | undefined;
  blocker: string | null | undefined;
  /** How many lines each may take before it is cut. */
  clamp?: 1 | 2;
  className?: string;
}) {
  const why = stripFactIds(reason);
  if (!why && !stripFactIds(blocker)) return null;
  return (
    <span className={cn("block min-w-0 space-y-0.5 text-[12.5px] leading-snug", className)}>
      {why && (
        <span className={cn("text-foreground/85", CLAMP[clamp])} title={why}>
          {why}
        </span>
      )}
      <Blocker clamp={clamp} text={blocker} />
    </span>
  );
}
