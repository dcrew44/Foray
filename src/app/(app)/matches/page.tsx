import { Columns3, LayoutList } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";

import { EmptyState, PageBody, PageHeader } from "@/components/foray/primitives";
import { StartSearchButton } from "@/components/home/live-panels";
import { RefreshOn } from "@/components/live/refresh-on";
import { Board } from "@/components/matches/board";
import { MatchTable } from "@/components/matches/match-table";
import { OPEN_STAGES } from "@/components/matches/stages";
import { buttonVariants } from "@/components/ui/button";
import type { components } from "@/lib/api/schema";
import { django } from "@/lib/api/server";
import { plural } from "@/lib/format";
import { cn } from "@/lib/utils";

export const metadata: Metadata = { title: "Matches" };

type MatchesPageData = components["schemas"]["MatchesPage"];
type SearchParams = Record<string, string | string[] | undefined>;

/** The address bar's own say in what the page shows; Django reads them as the page did. */
const PARAMS = ["view", "stage", "closed"] as const;

/**
 * The board or the list, in one read (`GET /api/pages/matches`). Django decides which from the
 * address bar's `view`, `stage` and `closed` (a stage link from Home lands on the list,
 * filtered) and returns the matches that view shows.
 */
export default async function MatchesPage({
  searchParams,
}: {
  searchParams: Promise<SearchParams>;
}) {
  const params = await searchParams;
  const query = new URLSearchParams();
  for (const key of PARAMS) {
    const value = params[key];
    const first = Array.isArray(value) ? value[0] : value;
    if (first !== undefined) query.set(key, first);
  }
  const path = query.size > 0 ? `/api/pages/matches?${query.toString()}` : "/api/pages/matches";
  const { counts, matches, ready, view, stage, showClosed } =
    await django<MatchesPageData>(path);

  const open = OPEN_STAGES.reduce((total, id) => total + counts[id], 0);
  const closed = counts.archived + counts.rejected;

  return (
    <PageBody width="full" className="max-w-[1600px]">
      <RefreshOn kinds={["run.completed", "approval.decided"]} />
      <PageHeader
        actions={
          <>
            <div className="bg-muted/60 flex h-8 items-center rounded-lg p-0.5" role="group" aria-label="View">
              <ViewLink active={view === "board"} href="/matches" icon={<Columns3 className="size-4" />} label="Board" />
              <ViewLink active={view === "list"} href="/matches?view=list" icon={<LayoutList className="size-4" />} label="List" />
            </div>
            {ready && <StartSearchButton label="Search again" variant="outline" />}
          </>
        }
        description={
          open > 0
            ? `${plural(open, "open match", "open matches")}, ${counts.shortlisted} shortlisted and ${counts.applied} applied. Drag cards between columns as things move.`
            : "Matches from your searches land here, scored against your resume."
        }
        title="Matches"
      />

      <div className="mt-8">
        {matches.length === 0 && open === 0 && !ready ? (
          <EmptyState
            action={
              <Link className={buttonVariants()} href="/home">
                Finish setting up
              </Link>
            }
            title="Set up your search first"
          >
            Foray needs your resume and a few answers about what you want before it can look for
            roles. It takes about five minutes.
          </EmptyState>
        ) : matches.length === 0 && open === 0 ? (
          <EmptyState action={<StartSearchButton label="Run a search" />} title="No matches yet">
            A search reads every open posting on the boards Foray tracks, scores the ones that fit
            your preferences, and puts them here.
          </EmptyState>
        ) : view === "board" ? (
          <>
            <Board matches={matches} showClosed={showClosed} />
            {closed > 0 && (
              <Link
                className="text-muted-foreground hover:text-foreground mt-2 inline-block text-[13px]"
                href={showClosed ? "/matches" : "/matches?closed=1"}
              >
                {showClosed ? "Hide rejected and archived" : `Show ${plural(closed, "rejected or archived match", "rejected or archived matches")}`}
              </Link>
            )}
          </>
        ) : (
          <MatchTable initialStage={stage} matches={matches} />
        )}
      </div>
    </PageBody>
  );
}

function ViewLink({
  href,
  label,
  icon,
  active,
}: {
  href: string;
  label: string;
  icon: React.ReactNode;
  active: boolean;
}) {
  return (
    <Link
      aria-current={active ? "page" : undefined}
      className={cn(
        "inline-flex h-7 items-center gap-1.5 rounded-md px-2.5 text-[13px]",
        active ? "bg-background font-medium shadow-sm" : "text-muted-foreground hover:text-foreground",
      )}
      href={href}
    >
      {icon}
      {label}
    </Link>
  );
}
