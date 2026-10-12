"use client";

import { useMemo, useState } from "react";

import { SignalDot } from "@/components/foray/primitives";
import { useLive } from "@/components/live/live-provider";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import type { LiveEvent } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import { eventTone } from "./run-summary";

/**
 * The event log: everything the agents reported, newest first and grouped by
 * day, straight from the app's one live feed. Debug chatter — per-batch
 * progress ticks — is hidden unless asked for.
 */

const PAGE = 100;

function dayKey(date: Date) {
  return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
}

function dayTitle(date: Date, now: Date): string {
  const today = dayKey(now);
  const yesterday = dayKey(new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1));
  if (dayKey(date) === today) return "Today";
  if (dayKey(date) === yesterday) return "Yesterday";
  return date.toLocaleDateString("en-US", {
    weekday: "long",
    month: "long",
    day: "numeric",
    ...(date.getFullYear() !== now.getFullYear() ? { year: "numeric" } : {}),
  });
}

export function ActivityFeed({
  showDebug,
  runLabel,
  onOpenRun,
}: {
  showDebug: boolean;
  /** What to call the run an event belongs to, e.g. "Search". */
  runLabel: (runId: string) => string;
  onOpenRun: (runId: string) => void;
}) {
  const { events, ready } = useLive();
  const [limit, setLimit] = useState(PAGE);

  const newestFirst = useMemo(
    () => events.filter((event) => showDebug || event.level !== "debug").reverse(),
    [events, showDebug],
  );
  const hiddenDebug = showDebug ? 0 : events.length - newestFirst.length;

  const groups = useMemo(() => {
    const now = new Date();
    const result: Array<{ key: string; title: string; events: LiveEvent[] }> = [];
    for (const event of newestFirst.slice(0, limit)) {
      const at = new Date(event.createdAt);
      const key = dayKey(at);
      const last = result.at(-1);
      if (last?.key === key) last.events.push(event);
      else result.push({ key, title: dayTitle(at, now), events: [event] });
    }
    return result;
  }, [newestFirst, limit]);

  if (!ready) {
    return (
      <div className="space-y-3 rounded-xl border px-4 py-4" aria-label="Loading the event log">
        <Skeleton className="h-4 w-2/3" />
        <Skeleton className="h-4 w-1/2" />
        <Skeleton className="h-4 w-3/5" />
      </div>
    );
  }

  if (newestFirst.length === 0) {
    return (
      <p className="text-muted-foreground rounded-xl border border-dashed px-5 py-4 text-[13.5px]">
        {hiddenDebug > 0
          ? "Only debug events so far. Turn on debug events to see them."
          : "Nothing yet. Searches, reviews and tailored resumes report here as they happen."}
      </p>
    );
  }

  return (
    <div className="space-y-3">
      <div className="divide-y overflow-hidden rounded-xl border">
        {groups.map((group) => (
          <section aria-label={group.title} key={group.key}>
            <h3 className="bg-muted/50 text-muted-foreground border-b px-4 py-1.5 text-[12.5px] font-medium">
              {group.title}
            </h3>
            <ol className="divide-y">
              {group.events.map((event, index) => (
                <EventRow
                  event={event}
                  key={event.id}
                  onOpenRun={onOpenRun}
                  // Name the run once per stretch of its events, not on every line.
                  runLabel={
                    event.runId && group.events[index - 1]?.runId !== event.runId
                      ? runLabel(event.runId)
                      : null
                  }
                />
              ))}
            </ol>
          </section>
        ))}
      </div>
      {newestFirst.length > limit && (
        <Button onClick={() => setLimit((current) => current + PAGE)} size="sm" variant="outline">
          Show older events
        </Button>
      )}
    </div>
  );
}

function EventRow({
  event,
  runLabel,
  onOpenRun,
}: {
  event: LiveEvent;
  runLabel: string | null;
  onOpenRun: (runId: string) => void;
}) {
  return (
    <li
      className={cn(
        "grid grid-cols-[4.25rem_minmax(0,1fr)] items-baseline gap-x-3 px-4 py-2.5 sm:grid-cols-[4.75rem_minmax(0,1fr)_auto]",
        event.level === "debug" && "text-muted-foreground",
      )}
      title={event.kind}
    >
      <time className="text-muted-foreground tabular text-[12.5px]" dateTime={event.createdAt}>
        {new Date(event.createdAt).toLocaleTimeString("en-US", {
          hour: "numeric",
          minute: "2-digit",
        })}
      </time>
      <p className="flex min-w-0 items-baseline gap-2.5 text-[13.5px] leading-snug">
        <SignalDot className="translate-y-[-1px]" tone={eventTone(event)} />
        <span className={cn("min-w-0 break-words", event.level === "error" && "text-destructive")}>
          {event.message}
        </span>
      </p>
      {event.runId && runLabel && (
        <button
          className="text-muted-foreground hover:text-foreground focus-visible:ring-ring/50 col-start-2 mt-1 w-fit rounded text-left text-[12.5px] underline-offset-4 outline-none hover:underline focus-visible:ring-2 sm:col-start-3 sm:mt-0"
          onClick={() => onOpenRun(event.runId!)}
          type="button"
        >
          {runLabel}
        </button>
      )}
    </li>
  );
}
