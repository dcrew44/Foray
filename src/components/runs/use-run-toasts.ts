"use client";

import { useEffect } from "react";
import { toast } from "sonner";

import { runToastId, useLiveEvent, watchRuns } from "@/components/live/live-provider";

/**
 * For a component that started a run and stays on screen while it works (a search button, a
 * deep review, a tailored resume): it tells the user about its own run (design §6.3).
 *
 * While the component is mounted the run is watched, so the shell's generic toasts for it are
 * held back, and its moments are toasted here instead: the outcome under the run's own toast id
 * (`runToastId`), and a pause for the user's go-ahead as the shell would show it. If the
 * component unmounts first, the shell toasts them instead, exactly once.
 */
export function useRunToasts(runId: string | null) {
  useEffect(() => (runId ? watchRuns([runId]) : undefined), [runId]);

  useLiveEvent(
    (event) => runId !== null && event.runId === runId,
    (event) => {
      if (!runId) return;
      switch (event.kind) {
        case "run.completed":
          toast.success(event.message, { id: runToastId(runId) });
          return;
        case "run.failed":
          toast.error(event.message, { id: runToastId(runId) });
          return;
        // Watching holds back the shell's own toast for this too, and a decision waiting on
        // the user is worth interrupting for wherever they are (the board has no approval card).
        case "approval.requested":
          toast(event.message, {
            id: `approval-${event.approvalId ?? event.id}`,
            description: "Review it on Home or in the chat that started it.",
            classNames: { toast: "foray-toast-signal" },
          });
          return;
      }
    },
  );
}
