"use client";

import { useRouter } from "next/navigation";
import { useRef, useState } from "react";

import { Button } from "@/components/ui/button";

type UploadResult = {
  usedVision?: boolean;
  counts?: Record<string, number>;
  error?: string;
};

export function ResumeUpload({ hasProfile }: { hasProfile: boolean }) {
  const router = useRouter();
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<UploadResult | null>(null);

  async function upload(file: File) {
    setBusy(true);
    setResult(null);

    const body = new FormData();
    body.append("file", file);

    const response = await fetch("/api/uploads", { method: "POST", body });
    const json = (await response.json()) as UploadResult;

    setBusy(false);
    setResult(json);
    if (response.ok) router.refresh();
  }

  return (
    <div className="space-y-3">
      <input
        accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        className="hidden"
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) void upload(file);
          event.target.value = "";
        }}
        ref={input}
        type="file"
      />

      <div className="flex items-center gap-3">
        <Button disabled={busy} onClick={() => input.current?.click()}>
          {busy
            ? "Reading your resume…"
            : hasProfile
              ? "Replace resume"
              : "Upload resume"}
        </Button>
        <span className="text-muted-foreground text-sm">PDF or .docx, up to 10 MB</span>
      </div>

      {result?.error && (
        <p className="text-destructive text-sm" role="alert">
          {result.error}
        </p>
      )}

      {result?.counts && (
        <p className="text-muted-foreground text-sm">
          Read {result.counts.work} roles, {result.counts.education} education
          entries, {result.counts.skills} skills and {result.counts.projects}{" "}
          projects
          {result.usedVision
            ? " — the text layer was unreadable, so the pages were read as images."
            : "."}{" "}
          Check them below and fix anything wrong.
        </p>
      )}
    </div>
  );
}
