"use client";

import { Plus, X } from "lucide-react";
import { useState, type FormEvent } from "react";
import { toast } from "sonner";

import { requestLiveRefresh, runToastId } from "@/components/live/live-provider";
import { useRunOutcome } from "@/components/profile/run-outcome";
import { RunCard } from "@/components/runs/run-card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { api, call } from "@/lib/api/client";
import type { CompanyAddOutput, CompanyRow, DiscoveryStep } from "@/lib/api/types";

/**
 * Adding a company by name, website or board link. Discovery checks the
 * careers page, guesses board names and verifies against the live API before
 * anything is registered, which takes a while — so it runs in the worker as a
 * `company_discovery` run. The form shows the run's live card while it works,
 * then the new row (highlighted in the table) or, on a miss, every step that
 * was tried. The outcome is toasted here, once.
 */

const STEP_LABEL: Record<string, string> = {
  "ats-link": "The board link you pasted",
  domain: "Website",
  "careers-page": "Careers page",
  "unsupported-ats": "Job board without a public API",
  homepage: "Homepage",
  "homepage-ats-link": "Board linked from the homepage",
  "slug-guess": "Guessed board names",
  search: "Web search",
  "search-ats-link": "Board found by web search",
  "careers-page-fallback": "Careers page with no known board",
};

export function AddCompany({ onAdded }: { onAdded: (company: CompanyRow) => void }) {
  const [value, setValue] = useState("");
  const [posting, setPosting] = useState(false);
  const [search, setSearch] = useState<{ query: string; runId: string } | null>(null);
  /** A stopped discovery's run: its card stays to say so until the next add. */
  const [stopped, setStopped] = useState<string | null>(null);
  const [miss, setMiss] = useState<{ message: string; trail: DiscoveryStep[] } | null>(null);

  useRunOutcome(search?.runId ?? null, (event) => {
    const query = search?.query ?? "";
    setSearch(null);
    const id = runToastId(event.runId ?? "");
    if (event.kind === "run.cancelled") {
      // The form is free again; the card says the search was stopped.
      setStopped(event.runId ?? null);
    } else if (event.kind === "run.completed") {
      const output = event.data as CompanyAddOutput | null;
      if (output?.registered && output.company) {
        toast.success(event.message, { id });
        setValue("");
        onAdded(output.company);
        return;
      }
      // The detail, and what was tried, sit under the field; the toast only says what happened.
      const trail = output?.trail ?? [];
      toast.error(trail.length > 0 ? `No job board found for “${query}”.` : event.message, { id });
      setMiss({ message: event.message, trail });
    } else if (event.kind === "run.failed") {
      toast.error(event.message, { id });
      setMiss({ message: event.message, trail: [] });
    }
  });

  const pending = posting || search !== null;

  async function submit(event: FormEvent) {
    event.preventDefault();
    const query = value.trim();
    if (!query || pending) return;

    setMiss(null);
    setStopped(null);
    setPosting(true);
    const result = await call(api.POST("/api/companies", { body: { query } }));
    setPosting(false);
    if (!result.ok) {
      toast.error(result.message);
      return;
    }
    setSearch({ query, runId: result.runId });
    requestLiveRefresh();
  }

  return (
    <div className="space-y-3">
      <form className="flex flex-col gap-2 sm:flex-row" onSubmit={submit}>
        <label className="sr-only" htmlFor="add-company">
          Company name, website or job board link
        </label>
        <Input
          autoComplete="off"
          className="h-9 flex-1 text-[13.5px] md:text-[13.5px]"
          disabled={pending}
          id="add-company"
          onChange={(event) => setValue(event.target.value)}
          placeholder="Company name, website or job board link"
          value={value}
        />
        <Button className="h-9" disabled={pending || !value.trim()} type="submit">
          {pending ? <Spinner /> : <Plus />}
          {pending ? "Looking…" : "Add company"}
        </Button>
      </form>

      {search && (
        <div className="space-y-2" role="status">
          <p className="text-muted-foreground text-[13px]">
            Looking for a job board for{" "}
            <span className="text-foreground font-medium">{search.query}</span>: checking its
            careers page, then Greenhouse, Lever and Ashby. This can take half a minute.
          </p>
          <RunCard compact runId={search.runId} />
        </div>
      )}

      {!search && stopped && <RunCard compact runId={stopped} />}

      {miss && (
        <div className="rounded-xl border px-4 py-3.5" role="alert">
          <div className="flex items-start justify-between gap-3">
            <p className="text-destructive text-[13.5px] font-medium">{miss.message}</p>
            <Button
              aria-label="Dismiss"
              className="text-muted-foreground -mt-1 -mr-2"
              onClick={() => setMiss(null)}
              size="icon-sm"
              variant="ghost"
            >
              <X />
            </Button>
          </div>
          {miss.trail.length > 0 && (
            <>
              <p className="text-muted-foreground mt-2 text-[12.5px]">What Foray tried, in order:</p>
              <ol className="mt-1.5 space-y-1.5 text-[13px]">
                {miss.trail.map((step, index) => (
                  <li className="grid gap-x-3 sm:grid-cols-[12rem_minmax(0,1fr)]" key={index}>
                    <span className="text-foreground/85">{STEP_LABEL[step.step] ?? step.step}</span>
                    <span className="text-muted-foreground min-w-0 break-words">{step.detail}</span>
                  </li>
                ))}
              </ol>
            </>
          )}
        </div>
      )}
    </div>
  );
}
