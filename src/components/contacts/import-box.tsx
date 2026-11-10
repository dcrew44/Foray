"use client";

import { Upload, Users } from "lucide-react";
import { useRouter } from "next/navigation";
import { useRef, useState, type DragEvent } from "react";
import { toast } from "sonner";

import { Spinner } from "@/components/ui/spinner";
import { api, call } from "@/lib/api/client";
import { contactsFileProblem } from "@/lib/contacts";
import { plural } from "@/lib/format";
import { cn } from "@/lib/utils";

const ACCEPT = ".csv,.zip,.tsv,.txt,text/csv,application/zip";

/**
 * Drop a list of people, get contacts.
 *
 * The import is one request: Django reads the file, links each person to the company they work
 * at when Foray reads that company's board, and answers with what changed. Nothing is queued
 * and nothing leaves Foray, so there is no run to follow: the sentence that comes back is the
 * outcome, and the page re-reads to show it.
 */
export function ImportBox({ hasContacts }: { hasContacts: boolean }) {
  const router = useRouter();
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [over, setOver] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  async function start(file: File) {
    // Refused here, before sending: a wrong type or an oversized file gets Django's own
    // sentence at once (past 16 MB the proxy would hang instead of answering 413).
    const problem = contactsFileProblem(file.name, file.size);
    if (problem) {
      setError(problem);
      setDone(null);
      return;
    }
    setError(null);
    setDone(null);
    setBusy(file.name);

    const form = new FormData();
    form.append("file", file);
    // openapi-fetch passes a FormData body through untouched and lets the browser set the
    // multipart boundary; the schema types the part as the `file` field it is.
    const result = await call(
      api.POST("/api/contacts/import", { body: form as unknown as { file: string } }),
    );
    setBusy(null);
    if (!result.ok) {
      setError(result.message);
      return;
    }
    toast.success(result.message);
    setDone(
      `${result.message} ${result.matched.toLocaleString()} of your ${plural(result.total, "contact")} ${result.matched === 1 ? "works" : "work"} at a company Foray reads.`,
    );
    router.refresh();
  }

  function onDrop(event: DragEvent) {
    event.preventDefault();
    setOver(false);
    const file = event.dataTransfer.files?.[0];
    if (file && !busy) void start(file);
  }

  return (
    <div>
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
          "group flex w-full items-center gap-4 rounded-xl border border-dashed px-5 py-5 text-left transition-colors",
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
          {busy ? <Spinner /> : hasContacts ? <Users className="size-5" /> : <Upload className="size-5" />}
        </span>
        <span className="min-w-0">
          <span className="block truncate text-[14px] font-medium">
            {busy
              ? `Reading ${busy}…`
              : hasContacts
                ? "Import another list, or a newer export"
                : "Drop your connections file here, or choose a file"}
          </span>
          <span className="text-muted-foreground block text-[12.5px]">
            {busy
              ? "Matching each person to the company they work at."
              : "LinkedIn's Connections.csv or the archive it comes in, or any CSV with a name column. Up to 10 MB."}
          </span>
        </span>
      </button>
      {done && (
        <p className="text-muted-foreground mt-2 text-[13px]" role="status">
          {done}
        </p>
      )}
      {error && (
        <p className="text-destructive mt-2 text-[13px]" role="alert">
          {error}
        </p>
      )}

      <details className="mt-3 text-[13px]">
        <summary className="text-muted-foreground hover:text-foreground cursor-pointer select-none">
          How to get your connections from LinkedIn
        </summary>
        <div className="text-muted-foreground mt-2 max-w-[66ch] space-y-2 leading-relaxed">
          <ol className="list-decimal space-y-1 pl-5">
            <li>
              On LinkedIn, open Settings &amp; Privacy, then Data privacy, then Get a copy of your
              data.
            </li>
            <li>Choose the option to pick what you want, tick Connections, and request the archive.</li>
            <li>
              LinkedIn emails you a download link, usually within ten minutes. Upload the archive
              here, or the Connections.csv inside it.
            </li>
          </ol>
          <p>
            Foray reads the file you give it and nothing else: it never signs in to LinkedIn and
            never looks anyone up. Any other list works too, such as an alumni directory or a
            spreadsheet of former colleagues, as long as it is a CSV with a name column. Company,
            title, email and school columns are read when they are there.
          </p>
        </div>
      </details>
    </div>
  );
}
