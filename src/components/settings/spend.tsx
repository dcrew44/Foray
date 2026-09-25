import { formatUsd, plural } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * What the agents have cost this month, read from the ledger. Server-safe: the
 * chart's hover and focus read-outs are plain CSS, so nothing here hydrates.
 */

export const PURPOSE_LABEL: Record<string, string> = {
  chat: "Chat",
  extract: "Reading resumes",
  score: "Scoring",
  evaluate: "Deep reviews",
  tailor: "Tailoring",
  verify: "Fact-checking",
  merge: "Merging imports",
};

export const PROVIDER_LABEL: Record<string, string> = {
  openai: "OpenAI",
  google: "Google",
  anthropic: "Anthropic",
  deepseek: "DeepSeek",
  compat: "OpenAI-compatible",
};

export type DaySpend = { day: string; usd: number | null };

/** Every day of the month, with null for the days still to come. */
export function monthDays(
  start: Date,
  rows: Array<{ day: string; usd: number }>,
  now = new Date(),
): DaySpend[] {
  const byDay = new Map(rows.map((row) => [row.day, row.usd]));
  const year = start.getUTCFullYear();
  const month = start.getUTCMonth();
  const count = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
  const today = now.toISOString().slice(0, 10);

  return Array.from({ length: count }, (_, index) => {
    const day = new Date(Date.UTC(year, month, index + 1)).toISOString().slice(0, 10);
    return { day, usd: day > today ? null : (byDay.get(day) ?? 0) };
  });
}

/** 0.45 → 0.5, 3.2 → 4, 12 → 15: a clean top for the axis. */
function niceCeiling(value: number): number {
  if (value <= 0) return 1;
  const power = 10 ** Math.floor(Math.log10(value));
  for (const step of [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) {
    if (value <= step * power + 1e-12) return step * power;
  }
  return 10 * power;
}

function dayLabel(day: string, long = false): string {
  return new Date(`${day}T00:00:00Z`).toLocaleDateString("en-US", {
    timeZone: "UTC",
    month: long ? "long" : "short",
    day: "numeric",
  });
}

function axisUsd(value: number): string {
  if (value === 0) return "$0";
  if (value < 1) return `$${value.toFixed(2)}`;
  return `$${value.toFixed(value < 10 && value % 1 ? 1 : 0)}`;
}

/**
 * Daily spend as columns: one series, so one colour and no legend. The
 * busiest day carries its own label; the rest read out on hover or focus.
 */
export function SpendChart({ days }: { days: DaySpend[] }) {
  const values = days.map((day) => day.usd ?? 0);
  const peak = Math.max(0, ...values);
  const top = niceCeiling(peak);
  const peakIndex = peak > 0 ? values.indexOf(peak) : -1;
  const ticks = days
    .map((day, index) => ({ day, index }))
    .filter(({ index }) => index === 0 || (index + 1) % 7 === 1 || index === days.length - 1)
    .filter(({ index }, i, all) => i === all.length - 1 || all[i + 1]!.index - index > 2);

  return (
    <figure className="space-y-2">
      <div className="relative h-40 pr-10">
        {/* Recessive frame: a hairline at the axis top, and the baseline. */}
        <div className="border-border absolute inset-x-0 top-0 border-t" />
        <span className="text-muted-foreground tabular absolute top-0 right-0 -translate-y-1/2 bg-transparent text-[11.5px]">
          {axisUsd(top)}
        </span>
        <span className="text-muted-foreground tabular absolute right-0 bottom-0 translate-y-1/2 text-[11.5px]">
          $0
        </span>

        <ol className="absolute inset-y-0 right-10 left-0 flex items-end gap-[2px] border-b">
          {days.map((day, index) => {
            const usd = day.usd;
            const height = usd && top ? Math.max((usd / top) * 100, 1.5) : 0;
            const align =
              index < 3 ? "left-0" : index > days.length - 4 ? "right-0" : "left-1/2 -translate-x-1/2";
            const label =
              usd == null
                ? `${dayLabel(day.day, true)}: still to come`
                : `${dayLabel(day.day, true)}: ${usd > 0 ? formatUsd(usd) : "nothing spent"}`;

            return (
              <li
                aria-label={label}
                className="group/day relative flex h-full min-w-0 flex-1 items-end justify-center outline-none"
                key={day.day}
                tabIndex={usd ? 0 : undefined}
              >
                <span
                  className="relative flex w-full max-w-6 justify-center"
                  style={{ height: `${height}%` }}
                >
                  {usd != null && usd > 0 && (
                    <span className="bg-primary group-hover/day:bg-primary/75 group-focus-visible/day:ring-ring/60 absolute inset-0 rounded-t-[4px] transition-colors group-focus-visible/day:ring-2" />
                  )}
                  {index === peakIndex && (
                    <span className="text-foreground/80 tabular pointer-events-none absolute bottom-full mb-1 text-[11.5px] font-medium whitespace-nowrap group-hover/day:opacity-0 group-focus-visible/day:opacity-0">
                      {formatUsd(peak)}
                    </span>
                  )}
                  {usd != null && (
                    <span
                      className={cn(
                        "bg-foreground text-background pointer-events-none absolute bottom-full z-10 mb-1.5 rounded-md px-2 py-1 text-[12px] whitespace-nowrap opacity-0 shadow-sm transition-opacity",
                        "group-hover/day:opacity-100 group-focus-visible/day:opacity-100",
                        align,
                      )}
                    >
                      <span className="tabular font-semibold">
                        {usd > 0 ? formatUsd(usd) : "$0"}
                      </span>{" "}
                      <span className="opacity-75">{dayLabel(day.day)}</span>
                    </span>
                  )}
                </span>
              </li>
            );
          })}
        </ol>
      </div>

      <div className="text-muted-foreground tabular relative mr-10 h-4 text-[11.5px]">
        {ticks.map(({ day, index }) => (
          <span
            className={cn(
              "absolute whitespace-nowrap",
              // The end labels hang inward so they never spill past the plot.
              index === 0 ? "" : index === days.length - 1 ? "-translate-x-full" : "-translate-x-1/2",
            )}
            key={day.day}
            style={{
              left:
                index === 0
                  ? 0
                  : index === days.length - 1
                    ? "100%"
                    : `${((index + 0.5) / days.length) * 100}%`,
            }}
          >
            {dayLabel(day.day)}
          </span>
        ))}
      </div>

      <figcaption className="sr-only">
        Model spend by day this month.
        {days.filter((day) => (day.usd ?? 0) > 0).length === 0
          ? " Nothing spent yet."
          : days
              .filter((day) => (day.usd ?? 0) > 0)
              .map((day) => ` ${dayLabel(day.day, true)}: ${formatUsd(day.usd ?? 0)}.`)
              .join("")}
      </figcaption>
    </figure>
  );
}

export function PurposeTable({
  rows,
}: {
  rows: Array<{ purpose: string; usd: number; calls: number }>;
}) {
  const total = rows.reduce((sum, row) => sum + row.usd, 0);
  return (
    <table className="w-full text-[13.5px]">
      <caption className="sr-only">Spend by purpose this month</caption>
      <thead>
        <tr className="text-muted-foreground border-b text-left text-[12.5px]">
          <th className="py-2 pr-4 font-normal">Purpose</th>
          <th className="hidden w-[30%] py-2 pr-4 font-normal sm:table-cell">
            <span className="sr-only">Share of spend</span>
          </th>
          <th className="py-2 pr-4 text-right font-normal">Calls</th>
          <th className="py-2 text-right font-normal">Spend</th>
        </tr>
      </thead>
      <tbody className="divide-y">
        {rows.map((row) => (
          <tr key={row.purpose}>
            <td className="py-2.5 pr-4">{PURPOSE_LABEL[row.purpose] ?? row.purpose}</td>
            <td className="hidden py-2.5 pr-4 sm:table-cell">
              <span className="bg-muted block h-1.5 overflow-hidden rounded-full">
                <span
                  className="bg-primary block h-full rounded-full"
                  style={{ width: `${total > 0 ? Math.max((row.usd / total) * 100, row.usd > 0 ? 2 : 0) : 0}%` }}
                />
              </span>
            </td>
            <td className="tabular text-muted-foreground py-2.5 pr-4 text-right">
              {row.calls.toLocaleString()}
            </td>
            <td className="tabular py-2.5 text-right font-medium">{formatUsd(row.usd)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function ModelTable({
  rows,
}: {
  rows: Array<{
    provider: string;
    model: string;
    usd: number;
    calls: number;
    failures: number;
    avgLatencyMs: number;
  }>;
}) {
  return (
    <table className="w-full text-[13.5px]">
      <caption className="sr-only">Spend by model this month</caption>
      <thead>
        <tr className="text-muted-foreground border-b text-left text-[12.5px]">
          <th className="py-2 pr-4 font-normal">Model</th>
          <th className="py-2 pr-4 text-right font-normal">Calls</th>
          <th className="hidden py-2 pr-4 text-right font-normal sm:table-cell">Failed</th>
          <th className="hidden py-2 pr-4 text-right font-normal sm:table-cell">Avg. time</th>
          <th className="py-2 text-right font-normal">Spend</th>
        </tr>
      </thead>
      <tbody className="divide-y">
        {rows.map((row) => (
          <tr key={`${row.provider}:${row.model}`}>
            <td className="py-2.5 pr-4">
              <span className="block">{row.model}</span>
              <span className="text-muted-foreground block text-[12.5px]">
                {PROVIDER_LABEL[row.provider] ?? row.provider}
              </span>
            </td>
            <td className="tabular text-muted-foreground py-2.5 pr-4 text-right">
              {row.calls.toLocaleString()}
            </td>
            <td
              className={cn(
                "tabular hidden py-2.5 pr-4 text-right sm:table-cell",
                row.failures > 0 ? "text-destructive" : "text-muted-foreground",
              )}
            >
              {row.failures.toLocaleString()}
            </td>
            <td className="tabular text-muted-foreground hidden py-2.5 pr-4 text-right sm:table-cell">
              {formatLatency(row.avgLatencyMs)}
            </td>
            <td className="tabular py-2.5 text-right font-medium">{formatUsd(row.usd)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function formatLatency(ms: number): string {
  if (!ms) return "–";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)} s`;
}

/**
 * The job aggregator's monthly quota. The free tier allows 200 requests;
 * Foray stops itself short of that so a busy afternoon cannot empty a month.
 */
export function AggregatorMeter({
  used,
  budget,
  quota = 200,
  enabled,
  resetsOn,
}: {
  used: number;
  budget: number;
  quota?: number;
  enabled: boolean;
  resetsOn: string;
}) {
  const spent = used >= budget;
  return (
    <div className="space-y-2.5">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <p className="text-[13.5px] font-medium">Job aggregator</p>
        <p className="text-muted-foreground tabular text-[13px]">
          {enabled ? (
            <>
              <span className="text-foreground font-medium">{used}</span> of {quota} free requests
              used this month
            </>
          ) : (
            "Off"
          )}
        </p>
      </div>
      {enabled && (
        <div className="relative">
          <div
            aria-label={`${used} of ${quota} requests used`}
            aria-valuemax={quota}
            aria-valuemin={0}
            aria-valuenow={used}
            className="bg-muted h-2 overflow-hidden rounded-full"
            role="meter"
          >
            <div
              className={cn("h-full rounded-full", spent ? "bg-destructive" : "bg-primary")}
              style={{ width: `${Math.min(100, (used / quota) * 100)}%` }}
            />
          </div>
          <span
            aria-hidden
            className="bg-foreground/40 absolute -top-1 h-4 w-px"
            style={{ left: `${(budget / quota) * 100}%` }}
          />
        </div>
      )}
      <p className={cn("text-[12.5px] leading-snug", spent ? "text-destructive" : "text-muted-foreground")}>
        {!enabled
          ? "Searches read company boards only. Set JSEARCH_ENABLED and RAPIDAPI_KEY in .env to also search across companies Foray does not track yet."
          : spent
            ? `Foray's own limit of ${budget} is used up, so searches skip the aggregator until ${resetsOn}.`
            : `Each search uses a request or two to find companies Foray does not track yet. Foray stops at ${budget}, the line above, and the count resets on ${resetsOn}. ${plural(Math.max(budget - used, 0), "request")} left.`}
      </p>
    </div>
  );
}
