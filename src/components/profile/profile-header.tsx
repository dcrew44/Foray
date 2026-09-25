"use client";

import { Download, FileText } from "lucide-react";
import { useState, useTransition } from "react";
import { toast } from "sonner";

import { renderBaseResumeAction } from "@/app/actions/resumes";
import { PageHeader } from "@/components/foray/primitives";
import { isActiveRun, requestLiveRefresh, useRun } from "@/components/live/live-provider";
import { RunCard } from "@/components/runs/run-card";
import { Button, buttonVariants } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";

/**
 * The profile's header, with the base resume as a PDF: download the latest
 * render, or make one. A render runs in the worker, so starting one shows its
 * live card here until the PDF is ready.
 */
export function ProfileHeader({
  title,
  description,
  pdf,
  canRender,
}: {
  title: string;
  description: string;
  /** The newest rendered PDF of the base resume, if any. */
  pdf: { documentId: string; version: number; current: boolean } | null;
  canRender: boolean;
}) {
  const [runId, setRunId] = useState<string | null>(null);
  const [starting, startTransition] = useTransition();
  const run = useRun(runId);
  const rendering = starting || isActiveRun(run);

  function render() {
    startTransition(async () => {
      const result = await renderBaseResumeAction();
      if (!result.ok) {
        toast.error(result.message);
        return;
      }
      setRunId(result.runId);
      requestLiveRefresh();
    });
  }

  const actions = canRender && (
    <>
      {pdf && (
        <a
          className={buttonVariants({ variant: "outline" })}
          href={`/api/documents/${pdf.documentId}?download=1`}
          title={
            pdf.current
              ? "Your resume as it stands"
              : `Made from version ${pdf.version}, before your latest changes`
          }
        >
          <Download />
          Download PDF
        </a>
      )}
      <Button
        disabled={rendering}
        onClick={render}
        variant={pdf?.current ? "ghost" : "default"}
      >
        {rendering ? <Spinner /> : <FileText />}
        {rendering ? "Making the PDF…" : "Make a PDF"}
      </Button>
    </>
  );

  return (
    <>
      <PageHeader actions={actions || undefined} description={description} title={title} />
      {pdf && !pdf.current && !runId && (
        <p className="text-muted-foreground mt-3 text-[12.5px] sm:text-right">
          The PDF on file is from version {pdf.version}, before your latest edits.
        </p>
      )}
      {runId && (
        <div className="mt-6 max-w-3xl space-y-2">
          <RunCard runId={runId} />
          {run && !isActiveRun(run) && (
            <Button
              className="text-muted-foreground"
              onClick={() => setRunId(null)}
              size="sm"
              variant="ghost"
            >
              Dismiss
            </Button>
          )}
        </div>
      )}
    </>
  );
}
