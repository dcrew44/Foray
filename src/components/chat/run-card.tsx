"use client";

import { useEffect, useState } from "react";

/**
 * Live progress for a run started from chat.
 *
 * Polls rather than streams, for the same reason the activity feed does: a run
 * outlives the chat turn that started it, and progress has to survive a reload.
 */

type RunStatus = {
  id: string;
  status: "queued" | "running" | "awaiting_approval" | "completed" | "failed" | "cancelled";
  stats?: Record<string, number | undefined> | null;
  output?: Record<string, number> | null;
  error?: string | null;
};

const POLL_MS = 2000;
const DONE = new Set(["completed", "failed", "cancelled"]);

const LABELS: Record<string, string> = {
  boardsRefreshed: "boards refreshed",
  aggregatorJobs: "from the aggregator",
  companiesDiscovered: "new companies",
  candidates: "candidates",
  newMatches: "new matches",
  scored: "scored",
};

export function RunCard({ runId }: { runId: string }) {
  const [run, setRun] = useState<RunStatus | null>(null);

  useEffect(() => {
    let live = true;

    const poll = async () => {
      const response = await fetch(`/api/runs/${runId}`, { cache: "no-store" });
      if (!response.ok || !live) return;

      const body = (await response.json()) as RunStatus;
      setRun(body);
      if (DONE.has(body.status)) clearInterval(timer);
    };

    void poll();
    const timer = setInterval(() => void poll(), POLL_MS);

    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [runId]);

  if (!run) return null;

  const stats = Object.entries(run.stats ?? {}).filter(
    ([key, value]) => LABELS[key] && typeof value === "number",
  );

  return (
    <div className="border-border my-2 space-y-2 rounded-lg border p-3 text-sm">
      <div className="flex items-center gap-2">
        <StatusDot status={run.status} />
        <span className="font-medium capitalize">
          {run.status.replace("_", " ")}
        </span>
        {run.status === "running" && (
          <span className="text-muted-foreground text-xs">
            this takes a little while
          </span>
        )}
      </div>

      {stats.length > 0 && (
        <dl className="text-muted-foreground grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:grid-cols-3">
          {stats.map(([key, value]) => (
            <div className="flex justify-between gap-2" key={key}>
              <dt>{LABELS[key]}</dt>
              <dd className="text-foreground tabular-nums">{value}</dd>
            </div>
          ))}
        </dl>
      )}

      {run.error && (
        <p className="text-destructive text-xs" role="alert">
          {run.error}
        </p>
      )}
    </div>
  );
}

function StatusDot({ status }: { status: RunStatus["status"] }) {
  const tone =
    status === "completed"
      ? "bg-emerald-500"
      : status === "failed"
        ? "bg-destructive"
        : status === "cancelled"
          ? "bg-muted-foreground"
          : "bg-amber-500 animate-pulse";

  return <span aria-hidden className={`size-2 rounded-full ${tone}`} />;
}
