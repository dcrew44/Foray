"use client";

import { Search } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo, useState, useTransition } from "react";
import { toast } from "sonner";

import { EmptyState, ScoreChip, VerdictPill } from "@/components/foray/primitives";
import { Input } from "@/components/ui/input";
import {
  formatLocation,
  formatSalary,
  STAGE_LABEL,
  timeAgo,
  type StageId,
} from "@/lib/format";
import { cn } from "@/lib/utils";

import type { BoardMatch } from "./board";
import { ClosedMark } from "./job-signals";
import { LaneLabel } from "./lane";
import { moveMatch } from "./match-api";
import { ALL_STAGES as STAGES } from "./stages";
import { Why } from "./verdict";

/**
 * The same matches as a sortable, filterable list — for when the question is
 * "which of these" rather than "where is each". Each row leads with its verdict,
 * one line of why and any blocker.
 */

/** The rubric bands a score can clear: plausible, worth applying, strong. */
const MIN_SCORES = [
  { label: "Any fit", value: 0 },
  { label: "55+", value: 55 },
  { label: "70+", value: 70 },
  { label: "85+", value: 85 },
];

export function MatchTable({
  matches,
  initialStage,
  laneFilter,
}: {
  matches: BoardMatch[];
  initialStage: StageId | null;
  /** The page's lane filter, which narrows `matches` itself: shown among the list's own. */
  laneFilter?: React.ReactNode;
}) {
  const router = useRouter();
  const [query, setQuery] = useState("");
  const [stage, setStage] = useState<StageId | "open" | "all">(initialStage ?? "open");
  const [minScore, setMinScore] = useState(0);
  const [reviewedOnly, setReviewedOnly] = useState(false);
  const [sort, setSort] = useState<"fit" | "newest">("fit");
  const [overrides, setOverrides] = useState<Record<string, StageId>>({});
  const [, startTransition] = useTransition();

  const rows = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const filtered = matches
      .map((match) => ({ ...match, stage: overrides[match.matchId] ?? match.stage }))
      .filter((match) => {
        if (stage === "open" && (match.stage === "rejected" || match.stage === "archived")) return false;
        if (stage !== "open" && stage !== "all" && match.stage !== stage) return false;
        if (minScore && (match.score ?? 0) < minScore) return false;
        if (reviewedOnly && match.deepScore == null) return false;
        if (needle && !`${match.title} ${match.company}`.toLowerCase().includes(needle)) return false;
        return true;
      });

    return filtered.sort((a, b) =>
      sort === "fit"
        ? (b.score ?? -1) - (a.score ?? -1)
        : new Date(b.postedAt ?? 0).getTime() - new Date(a.postedAt ?? 0).getTime(),
    );
  }, [matches, overrides, query, stage, minScore, reviewedOnly, sort]);

  function move(matchId: string, from: StageId, to: StageId) {
    setOverrides((previous) => ({ ...previous, [matchId]: to }));
    startTransition(async () => {
      const result = await moveMatch(matchId, to);
      if (!result.ok) {
        setOverrides((previous) => ({ ...previous, [matchId]: from }));
        toast.error(result.message ?? "That change did not save.");
        return;
      }
      router.refresh();
    });
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative w-full sm:w-64">
          <Search className="text-muted-foreground pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2" />
          <Input
            aria-label="Filter by title or company"
            className="h-8 pl-8"
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Title or company"
            value={query}
          />
        </div>
        <select
          aria-label="Stage"
          className="bg-background h-8 rounded-lg border px-2 text-[13px]"
          onChange={(event) => setStage(event.target.value as typeof stage)}
          value={stage}
        >
          <option value="open">All open stages</option>
          <option value="all">Every stage</option>
          {STAGES.map((id) => (
            <option key={id} value={id}>
              {STAGE_LABEL[id]}
            </option>
          ))}
        </select>
        {laneFilter}
        <div className="bg-muted/60 flex h-8 items-center rounded-lg p-0.5" role="group" aria-label="Minimum fit">
          {MIN_SCORES.map((option) => (
            <button
              aria-pressed={minScore === option.value}
              className={cn(
                "h-7 rounded-md px-2.5 text-[12.5px]",
                minScore === option.value ? "bg-background font-medium shadow-sm" : "text-muted-foreground hover:text-foreground",
              )}
              key={option.value}
              onClick={() => setMinScore(option.value)}
              type="button"
            >
              {option.label}
            </button>
          ))}
        </div>
        <label className="text-muted-foreground flex h-8 items-center gap-2 px-1 text-[13px]">
          <input
            checked={reviewedOnly}
            className="accent-[var(--primary)]"
            onChange={(event) => setReviewedOnly(event.target.checked)}
            type="checkbox"
          />
          Deep-reviewed only
        </label>
        <select
          aria-label="Sort"
          className="bg-background ml-auto h-8 rounded-lg border px-2 text-[13px]"
          onChange={(event) => setSort(event.target.value as typeof sort)}
          value={sort}
        >
          <option value="fit">Best fit first</option>
          <option value="newest">Newest first</option>
        </select>
      </div>

      {rows.length === 0 ? (
        <EmptyState title="Nothing matches these filters">
          Loosen the filters above, or run a new search from Home.
        </EmptyState>
      ) : (
        <div className="overflow-hidden rounded-xl border">
          <table className="w-full table-fixed text-[13.5px]">
            <thead className="bg-muted/40 text-muted-foreground text-left text-[12.5px]">
              <tr>
                <th className="w-16 py-2.5 pl-4 font-medium">Fit</th>
                <th className="py-2.5 pr-4 font-medium">Role</th>
                <th className="hidden w-[210px] py-2.5 pr-4 font-medium xl:table-cell">Location</th>
                <th className="hidden w-[128px] py-2.5 pr-4 font-medium md:table-cell">Pay</th>
                <th className="w-[148px] py-2.5 pr-4 font-medium">Stage</th>
                <th className="hidden w-[92px] py-2.5 pr-4 text-right font-medium sm:table-cell">Posted</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {rows.map((match) => (
                // Rows run to several lines, so cells hang from the top; the chip and the
                // stage select, taller than a line of text, sit a little higher to centre on the title.
                <tr className="hover:bg-muted/30 [&>td]:align-top" key={match.matchId}>
                  <td className="pt-1.5 pb-2.5 pl-4">
                    <ScoreChip evaluated={match.deepScore != null} score={match.score} size="sm" />
                  </td>
                  <td className="py-2.5 pr-4">
                    <Link className="group block min-w-0" href={`/matches/${match.matchId}`}>
                      {/* The verdict ends the title's line, so verdicts read down the list as a
                          column; on a phone it drops under the title instead of squeezing it. */}
                      <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 sm:flex-nowrap">
                        <span className="min-w-0 truncate font-medium group-hover:underline">{match.title}</span>
                        {/* The lane sits before the pill, so the pills still line up. */}
                        <span className="flex shrink-0 items-center gap-2 empty:hidden sm:ml-auto">
                          <LaneLabel lane={match.lane} />
                          <VerdictPill
                            recommendation={match.recommendation}
                            reviewed={match.deepScore != null}
                          />
                        </span>
                      </span>
                      <span className="text-muted-foreground mt-0.5 flex min-w-0 items-center gap-2 text-[12.5px]">
                        <span className="truncate">{match.company}</span>
                        {!match.isActive && <ClosedMark />}
                      </span>
                      <Why blocker={match.blocker} className="mt-1" reason={match.reason} />
                    </Link>
                  </td>
                  <td className="text-muted-foreground hidden truncate py-2.5 pr-4 xl:table-cell">
                    {formatLocation(match.locations, match.remote)}
                  </td>
                  <td className="tabular hidden py-2.5 pr-4 whitespace-nowrap md:table-cell">
                    {formatSalary(match) ?? <span className="text-muted-foreground">Not stated</span>}
                  </td>
                  <td className="pt-1.5 pb-2.5 pr-4">
                    <select
                      aria-label={`Stage for ${match.title}`}
                      className="hover:border-border h-7 rounded-md border border-transparent bg-transparent px-1.5 text-[13px]"
                      onChange={(event) => move(match.matchId, match.stage, event.target.value as StageId)}
                      value={match.stage}
                    >
                      {STAGES.map((id) => (
                        <option key={id} value={id}>
                          {STAGE_LABEL[id]}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td className="text-muted-foreground hidden py-2.5 pr-4 text-right whitespace-nowrap sm:table-cell">
                    {match.postedAt ? timeAgo(match.postedAt) : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="text-muted-foreground text-[12.5px]">
        {rows.length} of {matches.length} shown
      </p>
    </div>
  );
}
