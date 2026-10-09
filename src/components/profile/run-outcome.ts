"use client";

import { useEffect, useRef } from "react";

import { useLive, useRun, watchRuns } from "@/components/live/live-provider";
import type { LiveEvent, LiveRun } from "@/lib/api/types";

/** The events that end a run: exactly one per run, and always its last (R4). */
const ENDINGS = new Set(["run.completed", "run.failed", "run.cancelled"]);

/**
 * Follows a run this component started, and reports how it ended, once.
 *
 * - While mounted it registers the run with `watchRuns`, so the shell's generic outcome toast
 *   stays quiet: the caller toasts the outcome itself, with `runToastId(runId)`. If the user
 *   leaves first, the component unmounts and the generic toast fires instead, exactly once
 *   (design §6.3).
 * - The ending is looked up in the live window's events, not only among new ones, so a run that
 *   ended inside the shell's first snapshot (which never dispatches) is still reported.
 *
 * Used by the resume dropzone, adding a company, a company row's refresh and "Refresh stale
 * boards". Returns the run's latest state, or null until it is known.
 */
export function useRunOutcome(
  runId: string | null,
  onEnd: (event: LiveEvent) => void,
): LiveRun | null {
  const { events } = useLive();
  const run = useRun(runId);
  const reported = useRef<string | null>(null);
  const onEndRef = useRef(onEnd);

  useEffect(() => {
    onEndRef.current = onEnd;
  });

  useEffect(() => (runId ? watchRuns([runId]) : undefined), [runId]);

  useEffect(() => {
    if (!runId || reported.current === runId) return;
    const ending = events.find((event) => event.runId === runId && ENDINGS.has(event.kind));
    if (!ending) return;
    reported.current = runId;
    onEndRef.current(ending);
  }, [events, runId]);

  return run;
}
