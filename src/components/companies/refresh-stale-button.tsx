"use client";

import { RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { requestLiveRefresh, runToastId } from "@/components/live/live-provider";
import { useRunOutcome } from "@/components/profile/run-outcome";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { api, call } from "@/lib/api/client";

/** Stop showing the spinner after this long; the run's outcome still toasts when it lands. */
const PATIENCE_MS = 3 * 60 * 1000;

/**
 * Refreshes every active board not fetched in the last couple of hours. It
 * always starts a run, even when nothing is stale, so it always answers: the
 * start toast says what was queued, and the run's own outcome replaces it in
 * place ("Refreshed 23/23 boards — …", or "None of your boards are stale.").
 */
export function RefreshStaleButton() {
  const [posting, setPosting] = useState(false);
  const [runId, setRunId] = useState<string | null>(null);

  useRunOutcome(runId, (event) => {
    setRunId(null);
    const id = runToastId(event.runId ?? "");
    if (event.kind === "run.completed") toast.success(event.message, { id });
    else if (event.kind === "run.failed") toast.error(event.message, { id });
    // Stopped elsewhere (Activity, chat): the stop message replaces the start toast.
    else toast(event.message, { id });
  });

  useEffect(() => {
    if (!runId) return;
    const timer = setTimeout(() => setRunId(null), PATIENCE_MS);
    return () => clearTimeout(timer);
  }, [runId]);

  const waiting = posting || runId !== null;

  return (
    <Button
      disabled={waiting}
      onClick={async () => {
        setPosting(true);
        const result = await call(api.POST("/api/companies/refresh-stale"));
        setPosting(false);
        if (!result.ok) {
          toast.error(result.message);
          return;
        }
        toast(result.message, { id: runToastId(result.runId) });
        setRunId(result.runId);
        requestLiveRefresh();
      }}
      variant="outline"
    >
      {waiting ? <Spinner /> : <RefreshCw />}
      {waiting ? "Refreshing boards…" : "Refresh stale boards"}
    </Button>
  );
}
