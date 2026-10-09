"use client";

import { ChevronRight, MessageSquare } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { SignalDot } from "@/components/foray/primitives";
import { useLive } from "@/components/live/live-provider";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api/client";
import type { LiveEvent } from "@/lib/api/types";
import { formatDuration, formatUsd, timeAgo } from "@/lib/format";
import { cn } from "@/lib/utils";

import { eventTone, KIND_TITLE, runOutcome, STATUS, type RunSummary } from "./run-summary";

/**
 * Finished runs as compact rows. Each opens in place to show the events that
 * run wrote — fetched on demand, since last week's search is long gone from
 * the live window — with anything new from the live feed folded in.
 */
export function RunList({
  runs,
  open,
  onToggle,
}: {
  runs: RunSummary[];
  open: Set<string>;
  onToggle: (runId: string) => void;
}) {
  return (
    <div className="overflow-hidden rounded-xl border">
      <div
        aria-hidden
        className={cn(
          "bg-muted/40 text-muted-foreground hidden border-b px-4 py-2 text-[12.5px] sm:grid",
          COLUMNS,
        )}
      >
        <span />
        <span>Run</span>
        <span>Status</span>
        <span className="hidden text-right xl:block">Cost</span>
        <span className="text-right">Started</span>
        <span className="hidden text-right xl:block">Took</span>
      </div>
      <ol className="divide-y">
        {runs.map((summary) => (
          <RunRow
            key={summary.run.id}
            onToggle={() => onToggle(summary.run.id)}
            open={open.has(summary.run.id)}
            summary={summary}
          />
        ))}
      </ol>
    </div>
  );
}

/** One grid for the header and every row, so the columns line up. */
const COLUMNS =
  "grid-cols-[1rem_minmax(0,1fr)_auto] gap-x-3 sm:grid-cols-[1rem_minmax(0,1fr)_7.5rem_5rem] sm:gap-x-5 xl:grid-cols-[1rem_minmax(0,1fr)_7.5rem_4rem_5rem_4rem]";

function duration(run: RunSummary["run"]): string | null {
  if (!run.startedAt || !run.finishedAt) return null;
  return formatDuration(Math.max(0, Date.parse(run.finishedAt) - Date.parse(run.startedAt)));
}

function RunRow({
  summary,
  open,
  onToggle,
}: {
  summary: RunSummary;
  open: boolean;
  onToggle: () => void;
}) {
  const { run, target, costUsd } = summary;
  const status = STATUS[run.status];
  const detail =
    run.status === "failed"
      ? (run.error ?? "Stopped with an error")
      : run.status === "cancelled"
        ? "Stopped before it finished"
        : runOutcome(run);
  const took = duration(run);
  const started = timeAgo(run.startedAt ?? run.createdAt);
  const panelId = `run-${run.id}-events`;

  return (
    <li className="scroll-mt-24" id={`run-${run.id}`}>
      <button
        aria-controls={panelId}
        aria-expanded={open}
        className={cn(
          "grid w-full items-center px-4 py-3 text-left transition-colors outline-none",
          COLUMNS,
          "hover:bg-muted/40 focus-visible:bg-muted/50 focus-visible:ring-ring/40 focus-visible:ring-2 focus-visible:ring-inset",
          open && "bg-muted/30",
        )}
        onClick={onToggle}
        type="button"
      >
        <ChevronRight
          className={cn(
            "text-muted-foreground size-4 transition-transform duration-150",
            open && "rotate-90",
          )}
        />
        <span className="min-w-0">
          <span className="flex min-w-0 items-baseline gap-2">
            <span className="shrink-0 text-[13.5px] font-medium">
              {KIND_TITLE[run.kind] ?? "Run"}
            </span>
            {target && (
              <span className="text-muted-foreground truncate text-[13px]">{target}</span>
            )}
          </span>
          {detail ? (
            <span className="text-muted-foreground mt-0.5 block truncate text-[12.5px]">
              {detail}
              {/* Narrow screens drop the columns; carry the start time here. */}
              <span className="sm:hidden" suppressHydrationWarning>
                , {started}
              </span>
            </span>
          ) : (
            <span
              className="text-muted-foreground mt-0.5 block truncate text-[12.5px] sm:hidden"
              suppressHydrationWarning
            >
              {started}
            </span>
          )}
        </span>
        <span className="flex items-center justify-end gap-2 text-[13px] sm:justify-start">
          <SignalDot pulse={status.pulse} tone={status.tone} />
          <span
            className={cn(
              "hidden sm:inline",
              run.status === "failed" ? "text-destructive" : "text-foreground/85",
            )}
          >
            {status.label}
          </span>
          <span className="sr-only sm:hidden">{status.label}</span>
        </span>
        <span className="tabular text-muted-foreground hidden text-right text-[13px] xl:block">
          {costUsd != null && costUsd > 0 ? formatUsd(costUsd) : "–"}
        </span>
        {/* Relative times are recomputed on the client; a minute's drift is fine. */}
        <span
          className="text-muted-foreground hidden text-right text-[13px] sm:block"
          suppressHydrationWarning
        >
          {started}
        </span>
        <span className="tabular text-muted-foreground hidden text-right text-[13px] xl:block">
          {took ?? "–"}
        </span>
      </button>
      {open && <RunEvents id={panelId} summary={summary} />}
    </li>
  );
}

type Loaded = { state: "loading" } | { state: "ready"; events: LiveEvent[] } | { state: "error" };

function RunEvents({ summary, id }: { summary: RunSummary; id: string }) {
  const { run, costUsd, conversationId } = summary;
  const { events: liveEvents } = useLive();
  const [loaded, setLoaded] = useState<Loaded>({ state: "loading" });

  useEffect(() => {
    let current = true;
    api
      .GET("/api/runs/{id}/events", { params: { path: { id: run.id } }, cache: "no-store" })
      .then(({ data }) => {
        if (current) setLoaded(data ? { state: "ready", events: data.events } : { state: "error" });
      })
      .catch(() => {
        if (current) setLoaded({ state: "error" });
      });
    return () => {
      current = false;
    };
  }, [run.id]);

  // Anything the run wrote after the fetch arrives through the live feed.
  const events = useMemo(() => {
    const fetched = loaded.state === "ready" ? loaded.events : [];
    const byId = new Map(fetched.map((event) => [event.id, event]));
    for (const event of liveEvents) if (event.runId === run.id) byId.set(event.id, event);
    return [...byId.values()].sort((a, b) => a.id - b.id);
  }, [loaded, liveEvents, run.id]);

  const took = duration(run);
  const started = new Date(run.startedAt ?? run.createdAt);

  return (
    <div className="bg-muted/30 space-y-3 border-t px-4 pt-3 pb-4 sm:pl-12" id={id}>
      {run.status === "failed" && run.error && (
        <p className="text-destructive text-[13px] leading-relaxed">{run.error}</p>
      )}

      {loaded.state === "loading" && events.length === 0 ? (
        <div className="space-y-2" aria-label="Loading events">
          <Skeleton className="h-4 w-3/4" />
          <Skeleton className="h-4 w-1/2" />
          <Skeleton className="h-4 w-2/3" />
        </div>
      ) : loaded.state === "error" && events.length === 0 ? (
        <p className="text-muted-foreground text-[13px]">
          The events for this run could not be loaded. Close it and open it again to retry.
        </p>
      ) : events.length === 0 ? (
        <p className="text-muted-foreground text-[13px]">This run did not record any steps.</p>
      ) : (
        <ol className="space-y-1.5">
          {events.map((event) => (
            <li
              className={cn(
                "grid grid-cols-[4.75rem_0.5rem_minmax(0,1fr)] items-baseline gap-x-2.5 text-[13px] leading-snug",
                event.level === "debug" && "text-muted-foreground",
              )}
              key={event.id}
              title={event.kind}
            >
              <time className="text-muted-foreground tabular text-[12px]" dateTime={event.createdAt}>
                {new Date(event.createdAt).toLocaleTimeString("en-US", {
                  hour: "numeric",
                  minute: "2-digit",
                  second: "2-digit",
                })}
              </time>
              <SignalDot className="translate-y-[-1px]" tone={eventTone(event)} />
              <span
                className={cn(
                  "min-w-0 break-words",
                  event.level === "error" && "text-destructive",
                )}
              >
                {event.message}
              </span>
            </li>
          ))}
        </ol>
      )}

      <div className="text-muted-foreground flex flex-wrap items-center gap-x-4 gap-y-1 border-t pt-3 text-[12.5px]">
        <span>
          Started{" "}
          {started.toLocaleString("en-US", {
            month: "short",
            day: "numeric",
            hour: "numeric",
            minute: "2-digit",
          })}
        </span>
        {took && <span>Took {took}</span>}
        {costUsd != null && costUsd > 0 && <span>Cost {formatUsd(costUsd, { precise: true })}</span>}
        <span>{run.mode === "autopilot" ? "Autopilot" : "Asked before spending"}</span>
        {conversationId && (
          <Link
            className="text-primary inline-flex items-center gap-1 font-medium hover:underline"
            href={`/chat/${conversationId}`}
          >
            <MessageSquare className="size-3.5" />
            Open the chat that started it
          </Link>
        )}
      </div>
    </div>
  );
}
