"use client";

import { Radar } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";

import { requestLiveRefresh, runToastId } from "@/components/live/live-provider";
import { useRunOutcome } from "@/components/profile/run-outcome";
import { RunCard } from "@/components/runs/run-card";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { api, call } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { plural } from "@/lib/format";

type Lookup = components["schemas"]["ContactsLookup"];

/**
 * Looks for the job boards of the companies the user's contacts work at, a batch at a time.
 *
 * Only the user starts it: the button names how many companies it will ask about, and the list
 * under it says which. It starts the same `company_discovery` run as adding a company by hand,
 * so the run's card shows here while it works and its outcome is toasted once; the page then
 * re-reads, and the companies whose boards were found move up to the first list.
 */
export function LookupPanel({ lookup, unmatched }: { lookup: Lookup; unmatched: number }) {
  const router = useRouter();
  const [posting, setPosting] = useState(false);
  /** The run this panel started, until it ends. */
  const [started, setStarted] = useState<string | null>(null);
  /** A run stopped from its card: the card stays to say so. */
  const [stopped, setStopped] = useState<string | null>(null);
  // A lookup already in flight when the page was read (started in another tab, or before a
  // reload) is followed the same way.
  const runId = started ?? lookup.runId;

  useRunOutcome(runId, (event) => {
    setStarted(null);
    const id = runToastId(event.runId ?? "");
    if (event.kind === "run.cancelled") {
      setStopped(event.runId ?? null);
    } else if (event.kind === "run.completed") {
      if (event.level === "info") toast.success(event.message, { id });
      else toast(event.message, { id });
    } else if (event.kind === "run.failed") {
      toast.error(event.message, { id });
    }
    router.refresh();
  });

  const pending = posting || runId !== null;
  const count = lookup.names.length;

  async function start() {
    setStopped(null);
    setPosting(true);
    const result = await call(api.POST("/api/contacts/lookup"));
    setPosting(false);
    if (!result.ok) {
      toast.error(result.message);
      return;
    }
    setStarted(result.runId);
    requestLiveRefresh();
    router.refresh();
  }

  return (
    <div className="bg-card space-y-3 rounded-xl border px-5 py-4">
      <p className="text-muted-foreground max-w-[70ch] text-[13px] leading-relaxed">
        Foray can look for these companies&apos; job boards and start reading the ones it finds.
        It asks the job boards it knows, and web search when a key is set, about each company
        name. Company names are all it sends: never a person&apos;s name, email or profile.
      </p>
      {count > 0 ? (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <Button disabled={pending} onClick={start} variant="outline">
            {pending ? <Spinner /> : <Radar />}
            {pending
              ? "Looking for boards…"
              : `Look for the boards of ${plural(count, "company", "companies")}`}
          </Button>
          {lookup.remaining > count && !pending && (
            <span className="text-muted-foreground text-[12.5px]">
              The {count} most of your contacts work at, out of {lookup.remaining.toLocaleString()}{" "}
              not looked up yet.
            </span>
          )}
        </div>
      ) : (
        <p className="text-[13px]" role="status">
          {pending
            ? "Looking for boards…"
            : unmatched > 0
              ? "Every company here has been looked up in the last 30 days."
              : "Nothing to look up."}
        </p>
      )}
      {count > 0 && !pending && (
        <details className="text-[12.5px]">
          <summary className="text-muted-foreground hover:text-foreground cursor-pointer select-none">
            Which companies
          </summary>
          <p className="text-muted-foreground mt-1.5 max-w-[70ch] leading-relaxed">
            {lookup.names.join(", ")}
          </p>
        </details>
      )}
      {runId && <RunCard compact runId={runId} />}
      {!runId && stopped && <RunCard compact runId={stopped} />}
    </div>
  );
}
