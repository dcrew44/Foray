"use client";

import { FileText, Upload } from "lucide-react";
import { useRouter } from "next/navigation";
import { useRef, useState, type DragEvent } from "react";
import { toast } from "sonner";

import { requestLiveRefresh, runToastId } from "@/components/live/live-provider";
import { RunCard } from "@/components/runs/run-card";
import { Spinner } from "@/components/ui/spinner";
import { api, call } from "@/lib/api/client";
import { cn } from "@/lib/utils";

import { useRunOutcome } from "./run-outcome";

const ACCEPT =
  ".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document";

/**
 * Drop a resume, get a profile.
 *
 * The upload only stores the file and starts a `profile_import` run; the worker reads it, which
 * takes one model call, usually ten to thirty seconds. So the zone shows the run's live card,
 * then toasts the counts once ("Read 3 roles, 2 schools and 14 skills.") and refreshes the
 * page.
 */
export function ResumeDropzone({
  hasResume,
  className,
  compact = false,
}: {
  hasResume: boolean;
  className?: string;
  compact?: boolean;
}) {
  const router = useRouter();
  const input = useRef<HTMLInputElement>(null);
  /** The file being read, and its run once the upload has started one. */
  const [upload, setUpload] = useState<{ filename: string; runId: string | null } | null>(null);
  /** A stopped import's run: its card stays to say so until the next upload. */
  const [stopped, setStopped] = useState<string | null>(null);
  const [over, setOver] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useRunOutcome(upload?.runId ?? null, (event) => {
    setUpload(null);
    const id = runToastId(event.runId ?? "");
    if (event.kind === "run.cancelled") {
      // Nothing was saved, so there is nothing to refresh; the card says it was stopped.
      setStopped(event.runId ?? null);
    } else if (event.kind === "run.completed") {
      const output = event.data as { usedVision?: boolean } | null;
      toast.success(event.message, {
        id,
        description: output?.usedVision
          ? "The text layer was unreadable, so the pages were read as images. Worth a quick check."
          : "Check the details on your profile and fix anything that looks off.",
      });
      router.refresh();
    } else if (event.kind === "run.failed") {
      toast.error(event.message, { id });
    }
  });

  async function start(file: File) {
    if (!/\.(pdf|docx)$/i.test(file.name)) {
      setError("Upload a PDF or a Word (.docx) file.");
      return;
    }
    setError(null);
    setStopped(null);
    setUpload({ filename: file.name, runId: null });

    const form = new FormData();
    form.append("file", file);
    // openapi-fetch passes a FormData body through untouched and lets the browser set the
    // multipart boundary; the schema types the part as the `file` field it is.
    const result = await call(
      api.POST("/api/uploads", { body: form as unknown as { file: string } }),
    );
    if (!result.ok) {
      // Too large, the wrong type, too many uploads: Django's sentence, under the zone.
      setUpload(null);
      setError(result.message);
      return;
    }
    setUpload({ filename: file.name, runId: result.runId });
    requestLiveRefresh();
  }

  function onDrop(event: DragEvent) {
    event.preventDefault();
    setOver(false);
    const file = event.dataTransfer.files?.[0];
    if (file && !upload) void start(file);
  }

  const busy = upload?.filename ?? null;
  const cardRunId = upload?.runId ?? stopped;

  return (
    <div className={className}>
      <input
        accept={ACCEPT}
        className="hidden"
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) void start(file);
          event.target.value = "";
        }}
        ref={input}
        type="file"
      />
      <button
        className={cn(
          "group flex w-full items-center gap-4 rounded-xl border border-dashed text-left transition-colors",
          compact ? "px-4 py-3" : "px-5 py-5",
          over ? "border-primary bg-accent" : "hover:border-primary/50 hover:bg-muted/40",
          busy && "pointer-events-none",
        )}
        disabled={Boolean(busy)}
        onClick={() => input.current?.click()}
        onDragLeave={() => setOver(false)}
        onDragOver={(event) => {
          event.preventDefault();
          setOver(true);
        }}
        onDrop={onDrop}
        type="button"
      >
        <span className="bg-primary/10 text-primary inline-flex size-10 shrink-0 items-center justify-center rounded-lg">
          {busy ? <Spinner /> : hasResume ? <FileText className="size-5" /> : <Upload className="size-5" />}
        </span>
        <span className="min-w-0">
          <span className="block truncate text-[14px] font-medium">
            {busy
              ? `Reading ${busy}…`
              : hasResume
                ? "Replace your resume"
                : "Drop your resume here, or choose a file"}
          </span>
          <span className="text-muted-foreground block text-[12.5px]">
            {busy
              ? "Pulling out your roles, education and skills. This takes up to half a minute."
              : "PDF or Word, up to 10 MB. Foray builds your profile from it."}
          </span>
        </span>
      </button>
      {cardRunId && <RunCard className="mt-3" compact runId={cardRunId} />}
      {error && (
        <p className="text-destructive mt-2 text-[13px]" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
