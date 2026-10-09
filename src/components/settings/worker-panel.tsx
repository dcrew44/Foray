"use client";

import { Check, Send } from "lucide-react";
import { useEffect, useState } from "react";

import { SignalDot } from "@/components/foray/primitives";
import { requestLiveRefresh, useLive, useLiveEvent } from "@/components/live/live-provider";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { api, call } from "@/lib/api/client";
import { formatDuration, timeAgo } from "@/lib/format";

/**
 * Whether the background worker is alive, with a way to prove it: send a job
 * and watch its answer come back through the same feed every run uses. The
 * answer names the process that took it.
 */

type Probe =
  | { state: "idle" }
  | { state: "waiting"; sentAt: number; jobId: number | null }
  | { state: "answered"; ms: number; pid: number | null }
  | { state: "silent" }
  | { state: "failed"; message: string };

/** `worker.ping`'s data: the job it answered and the process that ran it. */
type PingAnswer = { jobId?: number; workerPid?: number } | null;

/** How long to wait for the answer before saying there was none. */
const PATIENCE_MS = 20_000;

const NOT_QUEUED = "The test job could not be queued. Check that the database is running.";

export function WorkerPanel() {
  const { worker, ready } = useLive();
  const [probe, setProbe] = useState<Probe>({ state: "idle" });

  useLiveEvent(["worker.ping"], (event) => {
    const answer = event.data as PingAnswer;
    setProbe((current) => {
      if (current.state !== "waiting") return current;
      // Another tab's test job is not this one's answer. Before the job id is known (the
      // answer beat the reply), any answer will do.
      if (current.jobId != null && answer?.jobId != null && answer.jobId !== current.jobId) {
        return current;
      }
      // When the worker wrote its answer, if the clocks agree; otherwise when
      // this page heard it, which is never less than the truth.
      const heard = Date.now() - current.sentAt;
      const written = new Date(event.createdAt).getTime() - current.sentAt;
      return {
        state: "answered",
        ms: written > 0 && written <= heard ? written : heard,
        pid: answer?.workerPid ?? null,
      };
    });
  });

  // While waiting, ask the shared poll to look sooner than its idle pace, and
  // give up after a while rather than spinning forever.
  useEffect(() => {
    if (probe.state !== "waiting") return;
    const nudge = setInterval(requestLiveRefresh, 1500);
    const giveUp = setTimeout(() => setProbe({ state: "silent" }), PATIENCE_MS);
    return () => {
      clearInterval(nudge);
      clearTimeout(giveUp);
    };
  }, [probe.state]);

  async function send() {
    const sentAt = Date.now();
    setProbe({ state: "waiting", sentAt, jobId: null });
    const result = await call(api.POST("/api/worker/ping"));
    if (!result.ok) {
      const message = (result as { message?: string }).message;
      setProbe({ state: "failed", message: message ?? NOT_QUEUED });
      return;
    }
    setProbe((current) =>
      current.state === "waiting" && current.sentAt === sentAt
        ? { ...current, jobId: result.jobId }
        : current,
    );
    requestLiveRefresh();
  }

  const online = worker?.online ?? false;

  return (
    <div className="divide-y rounded-xl border">
      <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3 px-5 py-4">
        <div className="min-w-0 space-y-1">
          <p className="flex items-center gap-2 text-[14px] font-medium">
            {!ready ? (
              <>
                <SignalDot tone="idle" />
                Checking…
              </>
            ) : online ? (
              <>
                <SignalDot tone="ok" />
                Online
              </>
            ) : (
              <>
                <SignalDot tone="error" />
                <span className="text-destructive">Offline</span>
              </>
            )}
          </p>
          <p className="text-muted-foreground text-[12.5px]">
            {!ready ? (
              "Reading the worker's heartbeat."
            ) : worker?.lastSeenAt ? (
              `Last heartbeat ${timeAgo(worker.lastSeenAt)}.`
            ) : (
              // A worker that stopped normally leaves no heartbeat behind (design §9.7), so
              // this covers both "never started" and "stopped".
              <>
                The worker is not running. Start it with{" "}
                <code className="text-foreground">pnpm dev:worker</code>.
              </>
            )}
          </p>
        </div>
        <Button
          disabled={!ready || probe.state === "waiting"}
          onClick={() => void send()}
          variant="outline"
        >
          {probe.state === "waiting" ? <Spinner /> : <Send />}
          {probe.state === "waiting" ? "Waiting for an answer…" : "Send a test job"}
        </Button>
      </div>

      {(probe.state !== "idle" || (ready && !online)) && (
        <div className="px-5 py-3.5 text-[13px] leading-relaxed" role="status">
          {probe.state === "answered" ? (
            <p className="flex items-center gap-2">
              <Check className="text-success size-4 shrink-0" />
              {probe.pid != null
                ? `Answered by process ${probe.pid} in ${probe.ms < 1000 ? "under a second" : formatDuration(probe.ms)}.`
                : `The worker answered in ${probe.ms < 1000 ? "under a second" : formatDuration(probe.ms)}.`}
            </p>
          ) : probe.state === "waiting" ? (
            <p className="text-muted-foreground">
              Sent. The answer arrives through the activity feed, the same way search progress
              does.
            </p>
          ) : probe.state === "failed" ? (
            <p className="text-destructive">{probe.message}</p>
          ) : probe.state === "silent" || !online ? (
            <p className="text-muted-foreground">
              {probe.state === "silent" ? "No answer after 20 seconds. " : ""}
              Searches, reviews and tailoring wait in the queue until the worker runs.{" "}
              {worker?.lastSeenAt ? (
                <>
                  Start it in a terminal with{" "}
                  <code className="text-foreground">pnpm dev:worker</code>, or run the app and
                  worker together with <code className="text-foreground">pnpm dev:all</code>.
                </>
              ) : (
                <>
                  You can also run the app and worker together with{" "}
                  <code className="text-foreground">pnpm dev:all</code>.
                </>
              )}
            </p>
          ) : null}
        </div>
      )}
    </div>
  );
}
