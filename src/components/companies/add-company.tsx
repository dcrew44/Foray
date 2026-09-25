"use client";

import { Plus, X } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import { toast } from "sonner";

import { addCompanyAction } from "@/app/actions/companies";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import type { DiscoveryStep } from "@/lib/companies/discovery";

import type { CompanyRow } from "./company-row";

/**
 * Adding a company by name, website or board link. Discovery checks the
 * careers page, guesses board names and verifies against the live API before
 * anything is registered, which takes a while — so the form says what it is
 * doing, and a miss shows every step that was tried.
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
  const [pending, setPending] = useState<{ query: string; startedAt: number } | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [miss, setMiss] = useState<{ message: string; trail: DiscoveryStep[] } | null>(null);

  useEffect(() => {
    if (!pending) return;
    const timer = setInterval(
      () => setElapsed(Math.round((Date.now() - pending.startedAt) / 1000)),
      1000,
    );
    return () => clearInterval(timer);
  }, [pending]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const query = value.trim();
    if (!query || pending) return;

    setMiss(null);
    setElapsed(0);
    setPending({ query, startedAt: Date.now() });
    try {
      const result = await addCompanyAction(query);
      if (result.ok) {
        toast.success(result.message);
        setValue("");
        onAdded(result.company);
      } else {
        // The detail, and what was tried, sit under the field; the toast only says what happened.
        toast.error(result.trail.length > 0 ? `No job board found for “${query}”.` : result.message);
        setMiss({ message: result.message, trail: result.trail });
      }
    } catch {
      toast.error("That request did not go through. Check your connection and try again.");
    } finally {
      setPending(null);
    }
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
          disabled={Boolean(pending)}
          id="add-company"
          onChange={(event) => setValue(event.target.value)}
          placeholder="Company name, website or job board link"
          value={value}
        />
        <Button className="h-9" disabled={Boolean(pending) || !value.trim()} type="submit">
          {pending ? <Spinner /> : <Plus />}
          {pending ? "Looking…" : "Add company"}
        </Button>
      </form>

      {pending && (
        <p
          className="text-muted-foreground flex flex-wrap items-center gap-x-2 text-[13px]"
          role="status"
        >
          <span>
            Looking for a job board for{" "}
            <span className="text-foreground font-medium">{pending.query}</span>: checking its
            careers page, then Greenhouse, Lever and Ashby. This can take half a minute.
          </span>
          {elapsed > 0 && <span className="tabular">{elapsed}s</span>}
        </p>
      )}

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
