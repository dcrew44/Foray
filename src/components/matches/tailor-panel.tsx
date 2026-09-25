"use client";

import { ChevronRight, CircleAlert, CircleCheck, Download, FileText, RotateCcw, WandSparkles } from "lucide-react";
import { useState, useTransition } from "react";
import { toast } from "sonner";

import { tailorResumeAction } from "@/app/actions/resumes";
import { requestLiveRefresh } from "@/components/live/live-provider";
import { RunCard } from "@/components/runs/run-card";
import { Button, buttonVariants } from "@/components/ui/button";
import { timeAgo } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * The resume Foray wrote for this role: a preview, what changed and why, and
 * whether every claim checked out against the real resume.
 */

type Change = {
  section: string;
  item_id?: string | null;
  before?: string | null;
  after?: string | null;
  reason: string;
};

type Unsupported = {
  section: string;
  text: string;
  reason: string;
  source?: "rule" | "model";
};

export type TailoredView = {
  id: string;
  documentId: string | null;
  createdAt: string;
  changes: Change[];
  verification: {
    passed?: boolean;
    unsupported?: Unsupported[];
    revised?: boolean;
  } | null;
};

const SECTION_LABEL: Record<string, string> = {
  summary: "Summary",
  work: "Experience",
  projects: "Projects",
  education: "Education",
  skills: "Skills",
};

export function TailorPanel({
  matchId,
  tailored,
  hasResume,
}: {
  matchId: string;
  tailored: TailoredView | null;
  hasResume: boolean;
}) {
  const [runId, setRunId] = useState<string | null>(null);
  // The resume on screen when a run was started. Once the page refreshes with
  // a different one, that run has delivered and the full view takes over.
  const [startedFrom, setStartedFrom] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();
  const [showChanges, setShowChanges] = useState(false);
  const delivered = runId !== null && (tailored?.id ?? null) !== startedFrom;

  function tailor() {
    startTransition(async () => {
      const result = await tailorResumeAction(matchId);
      if (!result.ok) {
        toast.error(result.message);
        return;
      }
      setStartedFrom(tailored?.id ?? null);
      setRunId(result.runId);
      requestLiveRefresh();
    });
  }

  if (!hasResume) {
    return (
      <p className="text-muted-foreground text-[13.5px]">
        Upload your resume on your profile and Foray can write a version for this role.
      </p>
    );
  }

  if (runId && !delivered) {
    return <RunCard runId={runId} />;
  }

  if (!tailored) {
    return (
      <div className="space-y-3">
        <p className="text-muted-foreground text-[13.5px] leading-relaxed">
          Foray rewrites your resume for this posting: it reorders and rephrases what is already
          true, then checks every claim against your real resume before making a PDF. It never
          adds skills or experience you do not have.
        </p>
        <Button disabled={pending} onClick={tailor}>
          <WandSparkles />
          {pending ? "Starting…" : "Tailor my resume"}
        </Button>
      </div>
    );
  }

  const unsupported = tailored.verification?.unsupported ?? [];
  const passed = tailored.verification?.passed ?? unsupported.length === 0;
  const changes = tailored.changes ?? [];

  return (
    <div className="space-y-4">
      <div
        className={cn(
          "flex items-start gap-2.5 rounded-lg px-3 py-2.5 text-[13px]",
          passed ? "bg-fit-2/60 text-fit-2-foreground" : "bg-signal-soft text-signal-foreground",
        )}
      >
        {passed ? <CircleCheck className="mt-0.5 size-4 shrink-0" /> : <CircleAlert className="mt-0.5 size-4 shrink-0" />}
        <span>
          {passed
            ? `Every claim checked against your resume${tailored.verification?.revised ? ", after reverting a few it could not support" : ""}.`
            : `${unsupported.length} ${unsupported.length === 1 ? "claim needs" : "claims need"} your eye before you send this.`}
        </span>
      </div>

      <div className="overflow-hidden rounded-xl border bg-white">
        <iframe
          className="block h-[420px] w-full"
          sandbox=""
          src={`/api/resumes/${tailored.id}/preview`}
          title="Tailored resume preview"
        />
      </div>

      <div className="flex flex-wrap gap-2">
        {tailored.documentId ? (
          <>
            <a
              className={buttonVariants({ size: "sm" })}
              href={`/api/documents/${tailored.documentId}`}
              rel="noreferrer"
              target="_blank"
            >
              <FileText />
              Open PDF
            </a>
            <a
              className={buttonVariants({ size: "sm", variant: "outline" })}
              href={`/api/documents/${tailored.documentId}?download=1`}
            >
              <Download />
              Download
            </a>
          </>
        ) : (
          <span className="text-muted-foreground text-[12.5px]">The PDF did not render; the preview above is the same resume.</span>
        )}
        <Button disabled={pending} onClick={tailor} size="sm" variant="ghost">
          <RotateCcw />
          Tailor again
        </Button>
      </div>

      {unsupported.length > 0 && (
        <div className="space-y-2">
          <h3 className="text-[13.5px] font-semibold">Check these</h3>
          <ul className="space-y-2">
            {unsupported.map((item, index) => (
              <li className="border-signal/40 bg-signal-soft/40 rounded-lg border px-3 py-2 text-[13px]" key={index}>
                <p className="font-medium">&ldquo;{item.text}&rdquo;</p>
                <p className="text-muted-foreground mt-0.5">{item.reason}</p>
              </li>
            ))}
          </ul>
        </div>
      )}

      {changes.length > 0 && (
        <div>
          <button
            className="text-muted-foreground hover:text-foreground inline-flex items-center gap-1 text-[13px]"
            onClick={() => setShowChanges(!showChanges)}
            type="button"
          >
            <ChevronRight className={cn("size-4 transition-transform", showChanges && "rotate-90")} />
            {showChanges ? "Hide" : "Show"} what changed ({changes.length})
          </button>
          {showChanges && (
            <ol className="mt-3 space-y-3">
              {changes.map((change, index) => (
                <li className="border-l-2 pl-3 text-[13px]" key={index}>
                  <p className="text-muted-foreground text-[12px]">{SECTION_LABEL[change.section] ?? change.section}</p>
                  {change.before && (
                    <p className="text-muted-foreground mt-1 line-through decoration-1">{change.before}</p>
                  )}
                  {change.after && <p className="mt-1">{change.after}</p>}
                  <p className="text-muted-foreground mt-1 italic">{change.reason}</p>
                </li>
              ))}
            </ol>
          )}
        </div>
      )}

      <p className="text-muted-foreground text-[12px]">Made {timeAgo(tailored.createdAt)}</p>
    </div>
  );
}
