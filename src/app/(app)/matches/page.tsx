import { Columns3, LayoutList } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";

import { EmptyState, PageBody, PageHeader } from "@/components/foray/primitives";
import { StartSearchButton } from "@/components/home/live-panels";
import { RefreshOn } from "@/components/live/refresh-on";
import { Board } from "@/components/matches/board";
import { MatchTable } from "@/components/matches/match-table";
import { requireUser } from "@/lib/auth/session";
import { ALL_STAGES, countByStage, listMatches, OPEN_STAGES } from "@/lib/db/queries/matches";
import type { StageId } from "@/lib/format";
import { plural } from "@/lib/format";
import { cn } from "@/lib/utils";

export const metadata: Metadata = { title: "Matches" };

type SearchParams = { view?: string; stage?: string; closed?: string };

export default async function MatchesPage({
  searchParams,
}: {
  searchParams: Promise<SearchParams>;
}) {
  const user = await requireUser();
  const params = await searchParams;

  const stageParam = ALL_STAGES.includes(params.stage as StageId) ? (params.stage as StageId) : null;
  // A stage link from Home lands on the list, filtered, rather than the board.
  const view = params.view === "list" || stageParam ? "list" : "board";
  const showClosed = params.closed === "1";

  const [counts, matches] = await Promise.all([
    countByStage(user.id),
    listMatches(user.id, {
      stages: view === "board" && !showClosed ? OPEN_STAGES : ALL_STAGES,
      limit: 1000,
    }),
  ]);

  const open = OPEN_STAGES.reduce((total, stage) => total + counts[stage], 0);
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
            <StartSearchButton label="Search again" variant="outline" />
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
        {matches.length === 0 && open === 0 ? (
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
          <MatchTable initialStage={stageParam} matches={matches} />
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
