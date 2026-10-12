import { MapPin } from "lucide-react";
import Link from "next/link";

import {
  CompanyMark,
  RecommendationPill,
  ScoreChip,
} from "@/components/foray/primitives";
import type { MatchRow } from "@/lib/api/types";
import {
  formatLocation,
  formatSalary,
  STAGE_LABEL,
  timeAgo,
  type StageId,
} from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * One match on one line — the unit of every list in the app: Home's best
 * matches, a finished search's results, a chat reply listing roles.
 */

export type MatchLineData = {
  matchId: string | null;
  title: string;
  company: string;
  location: string;
  score: number | null;
  evaluated: boolean;
  recommendation: string | null;
  salary: string | null;
  postedAt: string | Date | null;
  stage?: StageId | null;
  headline?: string | null;
};

export function fromMatchRow(row: MatchRow): MatchLineData {
  return {
    matchId: row.matchId,
    title: row.title,
    company: row.company,
    location: formatLocation(row.locations, row.remote),
    score: row.score,
    evaluated: row.deepScore != null,
    recommendation: row.recommendation,
    salary: formatSalary(row),
    postedAt: row.postedAt,
    stage: row.stage,
    headline: row.headline,
  };
}

export function MatchLine({
  match,
  showStage = false,
  showHeadline = false,
  className,
}: {
  match: MatchLineData;
  showStage?: boolean;
  showHeadline?: boolean;
  className?: string;
}) {
  const body = (
    <>
      <ScoreChip evaluated={match.evaluated} score={match.score} />
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-center gap-2">
          <span className="truncate text-[14px] font-medium">{match.title}</span>
          <RecommendationPill className="hidden sm:inline-flex" value={match.recommendation} />
        </div>
        <div className="text-muted-foreground mt-0.5 flex min-w-0 items-center gap-3 text-[13px]">
          <span className="text-foreground/80 truncate">{match.company}</span>
          <span className="flex min-w-0 items-center gap-1 truncate">
            <MapPin className="size-3.5 shrink-0 opacity-60" />
            <span className="truncate">{match.location}</span>
          </span>
        </div>
        {showHeadline && match.headline && (
          <p className="text-foreground/85 mt-1.5 line-clamp-2 text-[13px] leading-snug">
            {match.headline}
          </p>
        )}
      </div>
      <div className="text-muted-foreground hidden shrink-0 flex-col items-end gap-0.5 text-right text-[12.5px] sm:flex">
        {showStage && match.stage ? (
          <span className="text-foreground/80">{STAGE_LABEL[match.stage]}</span>
        ) : (
          match.salary && <span className="text-foreground/80 tabular">{match.salary}</span>
        )}
        {match.postedAt && <span>{timeAgo(match.postedAt)}</span>}
      </div>
    </>
  );

  const classes = cn(
    "flex items-start gap-3.5 rounded-lg px-3 py-3 transition-colors",
    match.matchId && "hover:bg-muted/70",
    className,
  );

  return match.matchId ? (
    <Link className={classes} href={`/matches/${match.matchId}`}>
      {body}
    </Link>
  ) : (
    <div className={classes}>{body}</div>
  );
}

/** A company-first line, for places where the company matters more than fit. */
export function CompanyLine({ name, detail }: { name: string; detail?: string }) {
  return (
    <div className="flex items-center gap-3">
      <CompanyMark name={name} size="sm" />
      <span className="min-w-0 truncate text-[13.5px] font-medium">{name}</span>
      {detail && <span className="text-muted-foreground text-[12.5px]">{detail}</span>}
    </div>
  );
}
