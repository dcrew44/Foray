"use client";

import { FileText, Upload } from "lucide-react";
import { useRouter } from "next/navigation";
import { useRef, useState, type DragEvent } from "react";
import { toast } from "sonner";

import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/utils";

type UploadResult = {
  usedVision?: boolean;
  counts?: Record<string, number>;
  error?: string;
};

const ACCEPT =
  ".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document";

/**
 * Drop a resume, get a profile. Reading it takes one model call — usually
 * ten to thirty seconds — so the zone says what is happening while it waits.
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
  const [busy, setBusy] = useState<string | null>(null);
  const [over, setOver] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function upload(file: File) {
    if (!/\.(pdf|docx)$/i.test(file.name)) {
      setError("Upload a PDF or a Word (.docx) file.");
      return;
    }
    setError(null);
    setBusy(file.name);

    const body = new FormData();
    body.append("file", file);

    try {
      const response = await fetch("/api/uploads", { method: "POST", body });
      const json = (await response.json()) as UploadResult;

      if (!response.ok || json.error) {
        setError(json.error ?? "That file could not be read.");
        return;
      }

      const counts = json.counts ?? {};
      toast.success(
        `Read ${counts.work ?? 0} roles, ${counts.education ?? 0} schools and ${counts.skills ?? 0} skills.`,
        {
          description: json.usedVision
            ? "The text layer was unreadable, so the pages were read as images. Worth a quick check."
            : "Check the details on your profile and fix anything that looks off.",
        },
      );
      router.refresh();
    } catch {
      setError("The upload did not go through. Check your connection and try again.");
    } finally {
      setBusy(null);
    }
  }

  function onDrop(event: DragEvent) {
    event.preventDefault();
    setOver(false);
    const file = event.dataTransfer.files?.[0];
    if (file) void upload(file);
  }

  return (
    <div className={className}>
      <input
        accept={ACCEPT}
        className="hidden"
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) void upload(file);
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
          <span className="block text-[14px] font-medium">
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
      {error && (
        <p className="text-destructive mt-2 text-[13px]" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
