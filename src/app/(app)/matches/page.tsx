import { Columns3, LayoutList } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";

import { EmptyState, PageBody, PageHeader } from "@/components/foray/primitives";
import { StartSearchButton } from "@/components/home/live-panels";
import { RefreshOn } from "@/components/live/refresh-on";
import { Board } from "@/components/matches/board";
import { LaneFilter } from "@/components/matches/lane-filter";
import { MatchTable } from "@/components/matches/match-table";
import { OPEN_STAGES } from "@/components/matches/stages";
import { buttonVariants } from "@/components/ui/button";
import type { components } from "@/lib/api/schema";
import { django } from "@/lib/api/server";
import { plural } from "@/lib/format";
import { matchesHref } from "@/lib/lanes";
import { cn } from "@/lib/utils";

export const metadata: Metadata = { title: "Matches" };

type MatchesPageData = components["schemas"]["MatchesPage"];
type SearchParams = Record<string, string | string[] | undefined>;

/** The address bar's own say in what the page shows; Django reads them as the page did. */
const PARAMS = ["view", "stage", "closed", "lane"] as const;

/**
 * The board or the list, in one read (`GET /api/pages/matches`). Django decides which from the
 * address bar's `view`, `stage` and `closed` (a stage link from Home lands on the list,
 * filtered) and returns the matches that view shows, narrowed to one apply lane when `lane`
 * names one. The links here keep the lane, so it holds across the two views.
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
  const { counts, matches, ready, view, stage, showClosed, lane } =
    await django<MatchesPageData>(path);

  const open = OPEN_STAGES.reduce((total, id) => total + counts[id], 0);
  const closed = counts.archived + counts.rejected;
  // Nothing to show at all. A lane that happens to be empty is not that: it keeps its filter.
  const empty = matches.length === 0 && open === 0 && !lane;
  const laneFilter = <LaneFilter lane={lane} showClosed={showClosed} stage={stage} view={view} />;
  // The closed count is of every lane, so a board narrowed to one does not quote it.
  const showClosedLabel = lane
    ? "Show rejected and archived"
    : `Show ${plural(closed, "rejected or archived match", "rejected or archived matches")}`;

  return (
    <PageBody width="full" className="max-w-[1600px]">
      <RefreshOn kinds={["run.completed", "approval.decided"]} />
      <PageHeader
        actions={
          <>
            <div className="bg-muted/60 flex h-8 items-center rounded-lg p-0.5" role="group" aria-label="View">
              <ViewLink active={view === "board"} href={matchesHref({ view: "board", lane })} icon={<Columns3 className="size-4" />} label="Board" />
              <ViewLink active={view === "list"} href={matchesHref({ view: "list", lane })} icon={<LayoutList className="size-4" />} label="List" />
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
        {empty && !ready ? (
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
        ) : empty ? (
          <EmptyState action={<StartSearchButton label="Run a search" />} title="No matches yet">
            A search reads every open posting on the boards Foray tracks, scores the ones that fit
            your preferences, and puts them here.
          </EmptyState>
        ) : view === "board" ? (
          <>
            <div className="mb-3 flex items-center gap-2">{laneFilter}</div>
            {lane && matches.length === 0 ? (
              <EmptyState
                action={
                  <Link
                    className={buttonVariants({ variant: "outline" })}
                    href={matchesHref({ view: "board", closed: showClosed })}
                  >
                    Show every lane
                  </Link>
                }
                title={showClosed ? "No matches in this lane" : "No open matches in this lane"}
              >
                A deep review puts a strong fit in the coached lane and a match worth applying to
                in the automated lane. You can move any match yourself on its page.
              </EmptyState>
            ) : (
              <Board matches={matches} showClosed={showClosed} />
            )}
            {closed > 0 && (
              <Link
                className="text-muted-foreground hover:text-foreground mt-2 inline-block text-[13px]"
                href={matchesHref({ view: "board", closed: !showClosed, lane })}
              >
                {showClosed ? "Hide rejected and archived" : showClosedLabel}
              </Link>
            )}
          </>
        ) : (
          <MatchTable initialStage={stage} laneFilter={laneFilter} matches={matches} />
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
