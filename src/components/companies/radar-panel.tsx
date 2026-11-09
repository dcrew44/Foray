import { Stat } from "@/components/foray/primitives";
import type { components } from "@/lib/api/schema";
import { formatLag, plural, timeAgo } from "@/lib/format";

import { BOARD_LABEL } from "./company-row";

type Radar = components["schemas"]["Radar"];

function sourceLabel(source: string): string {
  return (BOARD_LABEL as Record<string, string>)[source] ?? source;
}

/** How often boards are asked, in a sentence or two; null with no board to ask. */
function cadence(radar: Radar): string | null {
  const { hotBoards: hot, coldBoards: cold } = radar;
  if (hot + cold === 0) return null;
  if (cold === 0) {
    return `${plural(hot, "board is", "boards are")} checked every 15 minutes while the worker runs.`;
  }
  if (hot === 0) {
    return `${plural(cold, "board is", "boards are")} checked every hour while the worker runs. A board moves to every 15 minutes once it lists an early-career role or gives you a match.`;
  }
  return `${plural(hot, "board is", "boards are")} checked every 15 minutes while the worker runs: the ones that list early-career roles or have given a match. ${plural(cold, "more is", "more are")} checked every hour.`;
}

/**
 * How soon Foray sees a new posting, measured. A posting is timed when it
 * appears on a board that was checked before: the smaller of "since the board
 * says it was posted" and "since the last look, when it was not there". So the
 * number is what the radar actually achieved over the last week, worker
 * downtime included, not the polling interval it aims for.
 */
export function RadarPanel({ radar }: { radar: Radar }) {
  const boards = cadence(radar);

  return (
    <section aria-label="Radar" className="bg-card rounded-xl border px-5 py-4">
      {radar.medianS === null || radar.p90S === null ? (
        <div className="space-y-1">
          <p className="text-[13.5px] font-medium">No timings yet</p>
          <p className="text-muted-foreground max-w-[70ch] text-[13px] leading-relaxed">
            A new posting is timed when it shows up on a board Foray has checked before, so the
            first numbers arrive with the next one. {boards}
          </p>
        </div>
      ) : (
        <div className="grid gap-x-10 gap-y-5 lg:grid-cols-[auto_auto_minmax(0,1fr)] lg:items-start">
          <Stat label="Median time from posted to seen" value={formatLag(radar.medianS)} />
          <Stat label="Nine in ten seen within" value={formatLag(radar.p90S)} />
          <div className="min-w-0 space-y-2">
            <p className="text-muted-foreground max-w-[70ch] text-[13px] leading-relaxed">
              Measured on {plural(radar.postings, "new posting")} in the last {radar.days} days.{" "}
              {boards}
              {radar.lastCheckedAt ? ` Last check ${timeAgo(radar.lastCheckedAt)}.` : ""}
            </p>
            {radar.bySource.length > 1 && (
              <dl className="text-muted-foreground flex flex-wrap gap-x-6 gap-y-1 text-[12.5px]">
                {radar.bySource.map((item) => (
                  <div className="flex gap-1.5" key={item.source}>
                    <dt>{sourceLabel(item.source)}</dt>
                    <dd className="text-foreground tabular">{formatLag(item.medianS)}</dd>
                  </div>
                ))}
              </dl>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
