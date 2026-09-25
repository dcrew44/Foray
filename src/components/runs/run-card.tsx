"use client";

import { Check, CircleAlert, Download, FileText, OctagonX, RotateCcw } from "lucide-react";
import Link from "next/link";
import { useEffect, useState, useTransition } from "react";
import { toast } from "sonner";

import { cancelRunAction, startSearchAction } from "@/app/actions/runs";
import { Blaze } from "@/components/foray/primitives";
import { isActiveRun, requestLiveRefresh, useLive, useRun } from "@/components/live/live-provider";
import { fromMatchRow, MatchLine } from "@/components/matches/match-line";
import { Button, buttonVariants } from "@/components/ui/button";
import type { MatchRow } from "@/lib/db/queries/matches";
import { formatDuration, plural } from "@/lib/format";
import type { LiveRun } from "@/lib/runs/live";
import { cn } from "@/lib/utils";

import { ApprovalCard } from "./approval-card";

/**
 * A background run, live: what it is doing now, what it has found, and —
 * when it is waiting on the user — the decision itself, inline.
 */

type Step = { id: string; label: string; phases: string[] };

const SEARCH_STEPS: Step[] = [
  { id: "refresh", label: "Refresh boards", phases: ["starting", "refreshing", "aggregating"] },
  { id: "filter", label: "Filter", phases: ["filtering"] },
  { id: "score", label: "Score", phases: ["scoring"] },
  { id: "review", label: "Deep review", phases: ["awaiting_evaluation", "evaluating"] },
  { id: "shortlist", label: "Shortlist", phases: ["awaiting_shortlist", "shortlisting"] },
];

const TAILOR_STEPS: Step[] = [
  { id: "tailor", label: "Tailor", phases: ["starting", "tailoring"] },
  { id: "verify", label: "Verify claims", phases: ["verifying", "revising"] },
  { id: "render", label: "Render PDF", phases: ["rendering"] },
];

const KIND_TITLE: Record<string, string> = {
  search: "Search",
  rank_deep: "Deep review",
  tailor: "Tailored resume",
  resume_render: "Resume PDF",
  company_discovery: "Company discovery",
  company_refresh: "Board refresh",
};

function num(stats: Record<string, unknown>, key: string): number | null {
  const value = stats[key];
  return typeof value === "number" ? value : null;
}

/** What the run is doing right now, in words. */
function describe(run: LiveRun, workerOnline: boolean): string {
  const s = run.stats ?? {};
  const phase = typeof s.phase === "string" ? s.phase : null;

  if (run.status === "queued") {
    return workerOnline ? "Starting…" : "Waiting for the worker to start";
  }
  if (run.status === "awaiting_approval") return "Waiting for your go-ahead";
  if (run.status === "cancelled") return "Stopped. Everything found so far was kept.";
  if (run.status === "failed") return run.error ?? "Stopped with an error.";

  if (run.status === "completed") {
    const o = run.output ?? {};
    if (run.kind === "search") {
      const parts = [
        `${plural(Number(o.candidates ?? num(s, "candidates") ?? 0), "role")} matched your criteria`,
        o.scored != null && `${o.scored} scored`,
        Number(o.evaluated ?? 0) > 0 && `${o.evaluated} deep-reviewed`,
        Number(o.shortlisted ?? 0) > 0 && `${o.shortlisted} shortlisted`,
      ].filter(Boolean);
      return `${parts.join(", ")}.`;
    }
    if (run.kind === "tailor") {
      const unsupported = Number(o.unsupported ?? 0);
      return unsupported > 0
        ? `Ready, with ${plural(unsupported, "claim")} flagged for you to check.`
        : "Ready. Every claim checked against your resume.";
    }
    if (run.kind === "rank_deep") return `Reviewed ${plural(Number(o.evaluated ?? 0), "role")}.`;
    if (run.kind === "resume_render") return `Rendered to ${plural(Number(o.pages ?? 1), "page")}.`;
    return "Done.";
  }

  // running
  switch (phase) {
    case "refreshing":
      return num(s, "boardsRefreshed") != null
        ? `Refreshed ${num(s, "boardsRefreshed")} job boards`
        : "Refreshing job boards";
    case "aggregating":
      return "Checking the job aggregator for companies it does not track yet";
    case "filtering":
      return "Filtering open postings against your criteria";
    case "scoring": {
      const scored = num(s, "scored") ?? 0;
      const total = num(s, "candidates");
      return total ? `Scoring ${scored} of ${total} roles` : "Scoring roles";
    }
    case "evaluating": {
      const done = num(s, "evaluated") ?? 0;
      const total = num(s, "toEvaluate");
      if (total === 1) return "Reading the posting in full against your resume";
      return total ? `Reading ${done} of ${total} postings in full` : "Reading postings in full";
    }
    case "tailoring":
      return "Rewriting your resume for this role";
    case "verifying":
      return "Checking every claim against your resume";
    case "revising":
      return "Reverting claims it could not support";
    case "rendering":
      return "Rendering the PDF";
    default:
      return "Working…";
  }
}

function useElapsed(run: LiveRun | null): string | null {
  const active = isActiveRun(run);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [active]);

  if (!run?.startedAt) return null;
  const end = run.finishedAt ? new Date(run.finishedAt).getTime() : now;
  return formatDuration(Math.max(0, end - new Date(run.startedAt).getTime()));
}

export function RunCard({
  runId,
  className,
  compact = false,
}: {
  runId: string;
  className?: string;
  /** Hide results; used where the page already lists them. */
  compact?: boolean;
}) {
  const run = useRun(runId);
  const live = useLive();
  const elapsed = useElapsed(run);
  const [stopping, startStopping] = useTransition();

  if (!run) {
    return <div className={cn("bg-muted/40 h-[76px] animate-pulse rounded-xl border", className)} />;
  }

  const active = isActiveRun(run);
  const approval = live.approvals.find((item) => item.runId === run.id);
  const steps = run.kind === "search" ? SEARCH_STEPS : run.kind === "tailor" ? TAILOR_STEPS : null;
  const workerOnline = live.worker?.online ?? true;

  return (
    <div className={cn("bg-card overflow-hidden rounded-xl border", className)}>
      <div className="flex items-start gap-3 px-4 pt-3.5 pb-3">
        <StatusMark status={run.status} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span className="text-[14px] font-semibold">{KIND_TITLE[run.kind] ?? "Run"}</span>
            {run.kind === "search" && run.prompt && (
              <span className="text-muted-foreground max-w-full truncate text-[13px]">
                {run.prompt}
              </span>
            )}
          </div>
          <p
            className={cn(
              "mt-0.5 text-[13px] leading-snug",
              run.status === "failed" ? "text-destructive" : "text-foreground/80",
            )}
          >
            {describe(run, workerOnline)}
          </p>
        </div>
        <div className="text-muted-foreground flex shrink-0 items-center gap-2 text-[12px]">
          {run.kind === "search" && (
            <span className="bg-muted rounded-full px-2 py-0.5">
              {run.mode === "autopilot" ? "Autopilot" : "Asks first"}
            </span>
          )}
          {elapsed && <span className="tabular">{elapsed}</span>}
        </div>
      </div>

      {steps && (active || run.status === "completed") && (
        <StepTrack phase={String(run.stats?.phase ?? "")} status={run.status} steps={steps} />
      )}

      {approval && (
        <div className="px-3 pb-3">
          <ApprovalCard approval={approval} />
        </div>
      )}

      {!compact && run.status === "completed" && <RunResults run={run} />}

      {run.kind === "search" && (run.status === "failed" || run.status === "cancelled") && (
        <RunAgain prompt={run.prompt} />
      )}

      {active && !approval && (
        <div className="flex justify-end border-t px-3 py-2">
          <Button
            disabled={stopping}
            onClick={() =>
              startStopping(async () => {
                await cancelRunAction(run.id);
                requestLiveRefresh();
              })
            }
            size="xs"
            variant="ghost"
          >
            <OctagonX />
            Stop
          </Button>
        </div>
      )}
    </div>
  );
}

/**
 * Starting over is cheap: scoring skips what is already scored and the deep
 * review skips what is already reviewed, so a new run picks up where this one
 * stopped rather than paying twice.
 */
function RunAgain({ prompt }: { prompt: string | null }) {
  const [pending, startTransition] = useTransition();
  const [started, setStarted] = useState(false);

  if (started) {
    return (
      <p className="text-muted-foreground border-t px-4 py-2.5 text-[13px]">
        Started a new search. It skips anything already scored or reviewed.
      </p>
    );
  }

  return (
    <div className="flex items-center justify-between gap-3 border-t px-4 py-2.5">
      <span className="text-muted-foreground text-[12.5px]">
        Work already done is kept; a new run picks up the rest.
      </span>
      <Button
        disabled={pending}
        onClick={() =>
          startTransition(async () => {
            const result = await startSearchAction({ prompt: prompt ?? undefined });
            if (!result.ok) {
              toast.error(result.message);
              return;
            }
            setStarted(true);
            requestLiveRefresh();
          })
        }
        size="sm"
        variant="outline"
      >
        <RotateCcw />
        Run again
      </Button>
    </div>
  );
}

function StatusMark({ status }: { status: LiveRun["status"] }) {
  if (status === "completed") {
    return (
      <span className="bg-fit-4 text-fit-4-foreground mt-0.5 inline-flex size-6 shrink-0 items-center justify-center rounded-full">
        <Check className="size-3.5" strokeWidth={3} />
      </span>
    );
  }
  if (status === "failed") {
    return <CircleAlert className="text-destructive mt-0.5 size-6 shrink-0" />;
  }
  if (status === "cancelled") {
    return <OctagonX className="text-muted-foreground mt-0.5 size-6 shrink-0" />;
  }
  return (
    <span className="mt-0.5 inline-flex size-6 shrink-0 items-center justify-center">
      <Blaze live />
    </span>
  );
}

function StepTrack({
  steps,
  phase,
  status,
}: {
  steps: Step[];
  phase: string;
  status: LiveRun["status"];
}) {
  const current =
    status === "completed"
      ? steps.length
      : Math.max(0, steps.findIndex((step) => step.phases.includes(phase)));

  return (
    <ol className="flex items-center gap-1.5 px-4 pb-3.5" aria-label="Progress">
      {steps.map((step, index) => {
        const done = index < current;
        const now = index === current && status !== "completed";
        return (
          <li className="flex min-w-0 flex-1 flex-col gap-1.5" key={step.id}>
            <span
              className={cn(
                "h-1 rounded-full",
                done ? "bg-fit-4" : now ? "bg-signal animate-pulse" : "bg-muted",
              )}
            />
            <span
              className={cn(
                "truncate text-[11.5px]",
                done || now ? "text-foreground/80" : "text-muted-foreground/70",
              )}
            >
              {step.label}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

/** A finished run's payoff: the roles it found, or the resume it made. */
function RunResults({ run }: { run: LiveRun }) {
  const output = run.output ?? {};

  if (run.kind === "tailor" || run.kind === "resume_render") {
    const documentId = typeof output.documentId === "string" ? output.documentId : null;
    const matchId = typeof output.matchId === "string" ? output.matchId : null;
    return (
      <div className="flex flex-wrap gap-2 border-t px-4 py-3">
        {documentId && (
          <a
            className={buttonVariants({ size: "sm" })}
            href={`/api/documents/${documentId}`}
            rel="noreferrer"
            target="_blank"
          >
            <FileText />
            Open PDF
          </a>
        )}
        {documentId && (
          <a
            className={buttonVariants({ size: "sm", variant: "outline" })}
            href={`/api/documents/${documentId}?download=1`}
          >
            <Download />
            Download
          </a>
        )}
        {matchId && (
          <Link
            className={buttonVariants({ size: "sm", variant: "ghost" })}
            href={`/matches/${matchId}#resume`}
          >
            See what changed
          </Link>
        )}
      </div>
    );
  }

  const ids = Array.isArray(output.topMatchIds)
    ? (output.topMatchIds as string[])
    : Array.isArray(output.matchIds)
      ? (output.matchIds as string[])
      : [];

  if (ids.length === 0) return null;
  return <ResultList ids={ids} showAllLink={run.kind === "search"} />;
}

function ResultList({ ids, showAllLink }: { ids: string[]; showAllLink: boolean }) {
  const [rows, setRows] = useState<MatchRow[] | null>(null);
  const key = ids.join(",");

  useEffect(() => {
    let live = true;
    void fetch(`/api/matches?ids=${key}`, { cache: "no-store" })
      .then((response) => (response.ok ? response.json() : { matches: [] }))
      .then((body: { matches: MatchRow[] }) => {
        if (live) setRows(body.matches);
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [key]);

  if (!rows) return <div className="bg-muted/30 h-16 border-t" />;
  if (rows.length === 0) return null;

  return (
    <div className="border-t">
      <div className="px-1 py-1">
        {rows.map((row) => (
          <MatchLine key={row.matchId} match={fromMatchRow(row)} />
        ))}
      </div>
      {showAllLink && (
        <div className="border-t px-4 py-2.5">
          <Link className="text-primary text-[13px] font-medium hover:underline" href="/matches">
            Review all matches
          </Link>
        </div>
      )}
    </div>
  );
}
