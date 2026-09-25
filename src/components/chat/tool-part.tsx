"use client";

import { Check, ChevronRight, CircleAlert, CircleCheck } from "lucide-react";
import { useState } from "react";

import { CompanyMark } from "@/components/foray/primitives";
import { useLive } from "@/components/live/live-provider";
import { MatchLine, type MatchLineData } from "@/components/matches/match-line";
import { ApprovalCard } from "@/components/runs/approval-card";
import { RunCard } from "@/components/runs/run-card";
import { formatLocation, STAGE_LABEL, type StageId } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * How the agent's tool calls read in the conversation.
 *
 * The raw JSON is never the default: a started search becomes its live run
 * card, a list of jobs becomes match lines, a saved preference becomes one
 * quiet sentence. Anything without a bespoke rendering collapses to a single
 * line saying what the agent did, with the details a click away.
 */

type ToolState =
  | "input-streaming"
  | "input-available"
  | "approval-requested"
  | "approval-responded"
  | "output-available"
  | "output-error"
  | "output-denied";

export type ToolPartView = {
  name: string;
  state: ToolState;
  input: unknown;
  output: unknown;
  errorText?: string;
};

const COPY: Record<string, { running: string; done: string }> = {
  get_profile_summary: { running: "Checking your profile", done: "Checked your profile" },
  get_resume: { running: "Reading your resume", done: "Read your resume" },
  update_profile_preferences: { running: "Saving what you told me", done: "Saved your preferences" },
  add_company: { running: "Looking for the company's job board", done: "Checked the job board" },
  discover_companies: { running: "Thinking of companies that fit", done: "Queued companies to add" },
  list_companies: { running: "Checking which companies are tracked", done: "Checked the tracked companies" },
  start_search_run: { running: "Starting a search", done: "Started a search" },
  get_run_status: { running: "Checking on the run", done: "Checked on the run" },
  cancel_run: { running: "Stopping the run", done: "Stopped the run" },
  list_jobs: { running: "Looking through your matches", done: "Looked through your matches" },
  get_job: { running: "Reading the posting", done: "Read the posting" },
  move_job_stage: { running: "Updating your board", done: "Updated your board" },
  evaluate_job: { running: "Starting a deep review", done: "Started a deep review" },
  tailor_resume: { running: "Starting on a tailored resume", done: "Started a tailored resume" },
  list_pending_approvals: { running: "Checking what's waiting on you", done: "Checked what's waiting on you" },
  answer_approval: { running: "Recording your decision", done: "Recorded your decision" },
};

function copyFor(name: string) {
  return COPY[name] ?? { running: `Running ${name.replaceAll("_", " ")}`, done: `Ran ${name.replaceAll("_", " ")}` };
}

type Obj = Record<string, unknown>;
const isObj = (value: unknown): value is Obj => Boolean(value) && typeof value === "object";

export function ToolPart({ part }: { part: ToolPartView }) {
  const copy = copyFor(part.name);

  if (part.state === "input-streaming" || part.state === "input-available") {
    return <ActivityLine label={`${copy.running}…`} pending />;
  }

  if (part.state === "output-error") {
    return (
      <ActivityLine
        detail={part.errorText}
        icon={<CircleAlert className="text-destructive size-3.5" />}
        label={`${copy.running} failed`}
        tone="error"
      />
    );
  }

  if (part.state !== "output-available") {
    return <ActivityLine label={copy.done} />;
  }

  const output = isObj(part.output) ? part.output : {};

  // A tool that declined (missing preferences, unknown job) says why, plainly.
  if (output.ok === false) {
    return (
      <ActivityLine
        icon={<CircleAlert className="text-signal size-3.5" />}
        label={typeof output.message === "string" ? output.message : `${copy.running} did not work`}
      />
    );
  }

  // Anything that started background work gets its live card.
  if (typeof output.runId === "string") {
    return <RunCard runId={output.runId} />;
  }

  switch (part.name) {
    case "list_jobs":
      return <JobList output={output} />;
    case "get_job":
      return <JobPeek output={output} />;
    case "update_profile_preferences":
      return <PreferencesSaved input={part.input} output={output} />;
    case "add_company":
      return <CompanyAdded output={output} />;
    case "discover_companies":
      return (
        <ActivityLine
          icon={<CircleCheck className="text-success size-3.5" />}
          label={
            Array.isArray(output.queued)
              ? `Looking for job boards for ${(output.queued as string[]).join(", ")}. New ones appear in Activity as they are verified.`
              : copy.done
          }
        />
      );
    case "list_pending_approvals":
      return <PendingApprovals output={output} />;
    case "move_job_stage":
      return (
        <ActivityLine
          icon={<CircleCheck className="text-success size-3.5" />}
          label={
            typeof output.message === "string"
              ? output.message
              : typeof output.stage === "string" && output.stage in STAGE_LABEL
                ? `Moved to ${STAGE_LABEL[output.stage as StageId]}`
                : copy.done
          }
        />
      );
    case "answer_approval":
      return (
        <ActivityLine
          icon={<CircleCheck className="text-success size-3.5" />}
          label={typeof output.message === "string" ? output.message : copy.done}
        />
      );
    default:
      return <ActivityLine details={part.output} label={copy.done} />;
  }
}

/** One quiet line of agent activity, optionally expandable. */
function ActivityLine({
  label,
  detail,
  details,
  icon,
  pending = false,
  tone = "default",
}: {
  label: string;
  detail?: string;
  details?: unknown;
  icon?: React.ReactNode;
  pending?: boolean;
  tone?: "default" | "error";
}) {
  const [open, setOpen] = useState(false);
  const expandable = details !== undefined && details !== null;

  return (
    <div className="text-[13px]">
      <button
        className={cn(
          "text-muted-foreground inline-flex max-w-full items-start gap-2 text-left",
          expandable && "hover:text-foreground",
          !expandable && "cursor-default",
          tone === "error" && "text-destructive",
        )}
        disabled={!expandable}
        onClick={() => setOpen(!open)}
        type="button"
      >
        <span className="mt-[3px] inline-flex size-3.5 shrink-0 items-center justify-center">
          {pending ? (
            <span className="bg-signal animate-signal size-1.5 rounded-full" />
          ) : (
            (icon ?? <Check className="text-success size-3.5" />)
          )}
        </span>
        <span className={cn(pending && "animate-pulse")}>{label}</span>
        {expandable && (
          <ChevronRight className={cn("mt-[3px] size-3.5 shrink-0 transition-transform", open && "rotate-90")} />
        )}
      </button>
      {detail && <p className="text-muted-foreground mt-1 pl-5.5">{detail}</p>}
      {open && expandable && (
        <pre className="bg-muted/60 text-muted-foreground scrollbar-thin mt-2 max-h-64 overflow-auto rounded-lg p-3 text-[11.5px] leading-relaxed whitespace-pre-wrap">
          {JSON.stringify(details, null, 2)}
        </pre>
      )}
    </div>
  );
}

type JobCardOut = {
  matchId: string | null;
  title: string;
  company: string;
  locations?: string[];
  remote?: string;
  score?: number | null;
  fastScore?: number | null;
  deepScore?: number | null;
  recommendation?: string | null;
  headline?: string | null;
  salary?: string | null;
  postedAt?: string | null;
  stage?: StageId;
};

function toLine(job: JobCardOut): MatchLineData {
  return {
    matchId: job.matchId,
    title: job.title,
    company: job.company,
    location: formatLocation(job.locations, job.remote),
    // Conversations saved before scores were unified carry only fast/deep.
    score: job.score ?? job.deepScore ?? job.fastScore ?? null,
    evaluated: job.deepScore != null,
    recommendation: job.recommendation ?? null,
    salary: job.salary ?? null,
    postedAt: job.postedAt ?? null,
    stage: job.stage ?? null,
    headline: job.headline ?? null,
  };
}

function JobList({ output }: { output: Obj }) {
  const jobs = Array.isArray(output.jobs) ? (output.jobs as JobCardOut[]) : [];
  if (jobs.length === 0) {
    return <ActivityLine label="No matches fit that yet." />;
  }
  return (
    <div className="bg-card overflow-hidden rounded-xl border">
      <div className="p-1">
        {jobs.map((job, index) => (
          <MatchLine key={job.matchId ?? index} match={toLine(job)} showStage />
        ))}
      </div>
    </div>
  );
}

function JobPeek({ output }: { output: Obj }) {
  const match = isObj(output.match) ? output.match : null;
  const job: JobCardOut = {
    matchId: typeof output.matchId === "string" ? output.matchId : null,
    title: String(output.title ?? "Job"),
    company: String(output.company ?? ""),
    locations: Array.isArray(output.locations) ? (output.locations as string[]) : [],
    remote: typeof output.remote === "string" ? output.remote : undefined,
    score: match ? ((match.deepScore as number | null) ?? (match.fastScore as number | null) ?? null) : null,
    deepScore: match ? (match.deepScore as number | null) : null,
    recommendation:
      match && isObj(match.deepEval) && typeof match.deepEval.recommendation === "string"
        ? match.deepEval.recommendation
        : null,
    salary: typeof output.salary === "string" ? output.salary : null,
    postedAt: typeof output.postedAt === "string" ? output.postedAt : null,
    stage: match && typeof match.stage === "string" ? (match.stage as StageId) : undefined,
  };
  return (
    <div className="bg-card overflow-hidden rounded-xl border p-1">
      <MatchLine match={toLine(job)} showStage={Boolean(job.stage)} />
    </div>
  );
}

const PREFERENCE_LABEL: Record<string, string> = {
  target_roles: "target roles",
  seniority: "seniority",
  locations: "locations",
  remote_pref: "remote preference",
  salary_floor: "salary floor",
  work_auth: "work authorisation",
  start_date: "start date",
  must_haves: "must-haves",
  dealbreakers: "dealbreakers",
};

function PreferencesSaved({ input, output }: { input: unknown; output: Obj }) {
  const fields = isObj(input)
    ? Object.entries(input)
        .filter(([key, value]) => key in PREFERENCE_LABEL && value !== null && value !== undefined)
        .map(([key]) => PREFERENCE_LABEL[key])
    : [];
  const blocking = Array.isArray(output.blockingSearch) ? (output.blockingSearch as string[]) : [];

  return (
    <ActivityLine
      details={output.saved}
      icon={<CircleCheck className="text-success size-3.5" />}
      label={
        (fields.length ? `Saved your ${joinWords(fields)}.` : "Saved your preferences.") +
        (blocking.length === 0 && output.ok ? " Ready to search." : "")
      }
    />
  );
}

function CompanyAdded({ output }: { output: Obj }) {
  const name = typeof output.company === "string" ? output.company : "The company";
  const source = typeof output.source === "string" ? output.source.replace("_", " ") : null;
  return (
    <div className="bg-card flex items-center gap-3 rounded-xl border px-3 py-2.5 text-[13.5px]">
      <CompanyMark name={name} size="sm" />
      <span className="min-w-0 flex-1">
        <span className="font-medium">{name}</span>
        <span className="text-muted-foreground"> is now tracked{source ? ` via its ${source} board` : ""}.</span>
      </span>
      <CircleCheck className="text-success size-4 shrink-0" />
    </div>
  );
}

function PendingApprovals({ output }: { output: Obj }) {
  const { approvals } = useLive();
  const listed = Array.isArray(output.approvals) ? (output.approvals as Obj[]) : [];
  const ids = new Set(listed.map((item) => String(item.id)));
  const stillPending = approvals.filter((approval) => ids.has(approval.id));

  if (listed.length === 0) return <ActivityLine label="Nothing is waiting on you." />;
  // Answered since: the decision itself is the next line in the chat, and
  // "those decisions have been made" above it only repeated it vaguely.
  if (stillPending.length === 0) return null;

  return (
    <div className="space-y-3">
      {stillPending.map((approval) => (
        <ApprovalCard approval={approval} key={approval.id} />
      ))}
    </div>
  );
}

function joinWords(words: string[]): string {
  if (words.length <= 1) return words.join("");
  return `${words.slice(0, -1).join(", ")} and ${words.at(-1)}`;
}
