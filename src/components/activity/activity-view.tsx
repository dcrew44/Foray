"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Section } from "@/components/foray/primitives";
import { isActiveRun, useLive, useRun } from "@/components/live/live-provider";
import { RunCard } from "@/components/runs/run-card";
import { Switch } from "@/components/ui/switch";

import { ActivityFeed } from "./activity-feed";
import { RunList } from "./run-list";
import { KIND_TITLE, type RunSummary } from "./run-summary";

/**
 * The activity page's live half: runs in flight as full cards, finished ones
 * as rows that open to their events, and the event log underneath. The page
 * loads the finished runs; the shared live feed keeps everything current.
 */
export function ActivityView({ initialRuns }: { initialRuns: RunSummary[] }) {
  const live = useLive();
  const [open, setOpen] = useState<Set<string>>(() => new Set());
  const [showDebug, setShowDebug] = useState(false);
  // A run named in the event log but older than the list, opened on request.
  const [extraRunId, setExtraRunId] = useState<string | null>(null);
  const extraRun = useRun(extraRunId);
  const scrollTo = useRef<string | null>(null);

  const active = useMemo(
    () =>
      Object.values(live.runs)
        .filter(isActiveRun)
        .sort((a, b) => b.createdAt.localeCompare(a.createdAt)),
    [live.runs],
  );

  // The server's list, with fresher state from the live feed laid over it,
  // plus runs that finished since the page loaded.
  const finished = useMemo(() => {
    const byId = new Map<string, RunSummary>(
      initialRuns.map((summary) => [
        summary.run.id,
        { ...summary, run: live.runs[summary.run.id] ?? summary.run },
      ]),
    );
    for (const run of Object.values(live.runs)) {
      if (!byId.has(run.id)) {
        byId.set(run.id, { run, target: run.summary ?? run.prompt, costUsd: null, conversationId: null });
      }
    }
    if (extraRun && !byId.has(extraRun.id)) {
      byId.set(extraRun.id, { run: extraRun, target: extraRun.summary ?? extraRun.prompt, costUsd: null, conversationId: null });
    }
    return [...byId.values()]
      .filter((summary) => !isActiveRun(summary.run))
      .sort((a, b) => b.run.createdAt.localeCompare(a.run.createdAt));
  }, [initialRuns, live.runs, extraRun]);

  const toggle = useCallback((runId: string) => {
    setOpen((current) => {
      const next = new Set(current);
      if (next.has(runId)) next.delete(runId);
      else next.add(runId);
      return next;
    });
  }, []);

  const runLabel = useCallback(
    (runId: string) => {
      const run = live.runs[runId] ?? finished.find((summary) => summary.run.id === runId)?.run;
      return run ? (KIND_TITLE[run.kind] ?? "Run") : "View run";
    },
    [live.runs, finished],
  );

  const openRun = useCallback(
    (runId: string) => {
      const known = finished.some((summary) => summary.run.id === runId);
      if (!known && !live.runs[runId]) setExtraRunId(runId);
      setOpen((current) => new Set(current).add(runId));
      scrollTo.current = runId;
    },
    [finished, live.runs],
  );

  // Bring an opened run into view once its row exists — immediately for a run
  // in the list, or after an older one has been fetched.
  useEffect(() => {
    const runId = scrollTo.current;
    if (!runId) return;
    const row = document.getElementById(`run-${runId}`);
    if (!row) return;
    scrollTo.current = null;
    row.scrollIntoView({ block: "start", behavior: "smooth" });
  });

  return (
    <div className="space-y-14">
      <Section
        description={
          active.length > 0
            ? "Running now, then everything finished. Open a run to see each step it took."
            : "The last 30 runs. Open one to see each step it took."
        }
        title="Runs"
      >
        <div className="space-y-4">
          {active.length > 0 && (
            <div className="space-y-3">
              {active.map((run) => (
                <RunCard key={run.id} runId={run.id} />
              ))}
            </div>
          )}
          {finished.length > 0 ? (
            <RunList onToggle={toggle} open={open} runs={finished} />
          ) : (
            active.length === 0 && (
              <p className="text-muted-foreground rounded-xl border border-dashed px-5 py-4 text-[13.5px]">
                No runs yet. Ask Foray for roles, or start a search from Home, and it shows up
                here as it works.
              </p>
            )
          )}
        </div>
      </Section>

      <Section
        action={
          <label className="text-muted-foreground flex cursor-pointer items-center gap-2 text-[13px]">
            <Switch checked={showDebug} onCheckedChange={setShowDebug} size="sm" />
            Debug events
          </label>
        }
        description="Everything the agents reported, newest first."
        title="Event log"
      >
        <ActivityFeed onOpenRun={openRun} runLabel={runLabel} showDebug={showDebug} />
      </Section>
    </div>
  );
}
