"use client";

import {
  ArrowDown,
  ArrowUp,
  Ellipsis,
  ExternalLink,
  Pause,
  Play,
  RefreshCw,
  Search,
  X,
} from "lucide-react";
import { useCallback, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { refreshCompanyAction, setStatusAction } from "@/app/actions/companies";
import { CompanyMark } from "@/components/foray/primitives";
import { Segmented } from "@/components/profile/form-controls";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { timeAgo } from "@/lib/format";
import { cn } from "@/lib/utils";

import { AddCompany } from "./add-company";
import { BOARD_LABEL, FETCH_PROBLEM, type CompanyRow } from "./company-row";

export type { CompanyRow } from "./company-row";

/**
 * The registry, as one table: which companies Foray watches, where their
 * boards live, how many roles are open, and whether the last fetch worked.
 * Filtering and sorting happen here, on rows the page already loaded — a few
 * hundred rows is nothing to sort, and it keeps every keystroke instant.
 */

type Filter = "all" | "active" | "paused" | "invalid" | "browser";
type SortKey = "name" | "jobs" | "fetched";
type Sort = { key: SortKey; dir: "asc" | "desc" };

const FILTERS: Array<{ id: Filter; label: string; test: (row: CompanyRow) => boolean }> = [
  { id: "all", label: "All", test: () => true },
  { id: "active", label: "Active", test: (row) => row.status === "active" },
  { id: "paused", label: "Paused", test: (row) => row.status === "paused" },
  { id: "invalid", label: "Invalid", test: (row) => row.status === "invalid" },
  { id: "browser", label: "Needs a browser", test: (row) => row.needsBrowser },
];

const STATUS_RANK: Record<CompanyRow["status"], number> = { active: 0, paused: 1, invalid: 2 };

const NOTHING_IN: Record<Filter, string> = {
  all: "No companies yet.",
  active: "No active companies. Resume one to include it in searches again.",
  paused: "Nothing is paused.",
  invalid: "No invalid boards. Every board answered its last fetch.",
  browser: "No careers pages need a browser.",
};

function boardUrl(row: CompanyRow): string | null {
  if (row.sourceType === "careers_page") return row.careersUrl;
  if (!row.atsSlug) return null;
  switch (row.sourceType) {
    case "greenhouse":
      return `https://job-boards.greenhouse.io/${row.atsSlug}`;
    case "lever":
      return `https://jobs.lever.co/${row.atsSlug}`;
    case "ashby":
      return `https://jobs.ashbyhq.com/${row.atsSlug}`;
    default:
      return null;
  }
}

function boardDetail(row: CompanyRow): string | null {
  if (row.atsSlug) return row.atsSlug;
  if (row.careersUrl) {
    try {
      return new URL(row.careersUrl).hostname.replace(/^www\./, "");
    } catch {
      return row.careersUrl;
    }
  }
  return row.domain;
}

export function CompanyTable({ companies }: { companies: CompanyRow[] }) {
  // Rows the actions just returned, shown until the page's own data catches up
  // — and dropped once it does, so a later refresh is never masked by them.
  const [fresh, setFresh] = useState<Record<string, CompanyRow>>({});
  const [source, setSource] = useState(companies);
  if (source !== companies) {
    setSource(companies);
    setFresh({});
  }
  const [busy, setBusy] = useState<Set<string>>(() => new Set());
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [sort, setSort] = useState<Sort>({ key: "name", dir: "asc" });
  const [highlight, setHighlight] = useState<string | null>(null);
  const clearHighlight = useCallback(() => setHighlight(null), []);
  const deferredQuery = useDeferredValue(query);

  const rows = useMemo(() => {
    const bySlug = new Map(companies.map((row) => [row.slug, row]));
    for (const row of Object.values(fresh)) bySlug.set(row.slug, row);
    return [...bySlug.values()];
  }, [companies, fresh]);

  const counts = useMemo(
    () =>
      Object.fromEntries(
        FILTERS.map((option) => [option.id, rows.filter(option.test).length]),
      ) as Record<Filter, number>,
    [rows],
  );

  const visible = useMemo(() => {
    const needle = deferredQuery.trim().toLowerCase();
    const test = FILTERS.find((option) => option.id === filter)!.test;
    const matched = rows.filter(
      (row) =>
        test(row) &&
        (!needle ||
          [row.name, row.slug, row.atsSlug, row.domain, ...row.tags]
            .filter(Boolean)
            .some((text) => text!.toLowerCase().includes(needle))),
    );

    const direction = sort.dir === "asc" ? 1 : -1;
    const byName = (a: CompanyRow, b: CompanyRow) =>
      a.name.localeCompare(b.name, undefined, { sensitivity: "base" });

    return matched.sort((a, b) => {
      if (sort.key === "jobs") return direction * (a.activeJobs - b.activeJobs) || byName(a, b);
      if (sort.key === "fetched") {
        const at = a.lastFetchedAt ? Date.parse(a.lastFetchedAt) : null;
        const bt = b.lastFetchedAt ? Date.parse(b.lastFetchedAt) : null;
        if (at === bt) return byName(a, b);
        // Never-fetched boards sit at the end either way.
        if (at == null) return 1;
        if (bt == null) return -1;
        return direction * (at - bt);
      }
      return STATUS_RANK[a.status] - STATUS_RANK[b.status] || direction * byName(a, b);
    });
  }, [rows, deferredQuery, filter, sort]);

  function toggleSort(key: SortKey) {
    setSort((current) =>
      current.key === key
        ? { key, dir: current.dir === "asc" ? "desc" : "asc" }
        : { key, dir: key === "name" ? "asc" : "desc" },
    );
  }

  function remember(row: CompanyRow) {
    setFresh((current) => ({ ...current, [row.slug]: row }));
  }

  async function run(row: CompanyRow, action: "fetch" | "pause" | "resume") {
    setBusy((current) => new Set(current).add(row.slug));
    try {
      const result =
        action === "fetch"
          ? await refreshCompanyAction(row.slug)
          : await setStatusAction(row.slug, action === "pause" ? "paused" : "active");
      if ("company" in result && result.company) remember(result.company);
      if (result.ok) toast.success(result.message);
      else toast.error(result.message);
    } catch {
      toast.error("That request did not go through. Check your connection and try again.");
    } finally {
      setBusy((current) => {
        const next = new Set(current);
        next.delete(row.slug);
        return next;
      });
    }
  }

  return (
    <div className="space-y-6">
      <AddCompany
        onAdded={(row) => {
          remember(row);
          setQuery("");
          setFilter("all");
          setHighlight(row.slug);
        }}
      />

      <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
        <div className="relative w-full md:max-w-xs">
          <Search className="text-muted-foreground pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2" />
          <Input
            aria-label="Search companies"
            className="h-9 pr-8 pl-8 text-[13.5px] md:text-[13.5px] [&::-webkit-search-cancel-button]:hidden"
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search companies, boards or tags"
            type="search"
            value={query}
          />
          {query && (
            <button
              aria-label="Clear search"
              className="text-muted-foreground hover:text-foreground absolute top-1/2 right-2 inline-flex size-5 -translate-y-1/2 items-center justify-center rounded"
              onClick={() => setQuery("")}
              type="button"
            >
              <X className="size-3.5" />
            </button>
          )}
        </div>
        <div className="-mx-5 overflow-x-auto px-5 [scrollbar-width:none] sm:mx-0 sm:px-0 [&::-webkit-scrollbar]:hidden">
          <Segmented
            ariaLabel="Show companies"
            className="flex-nowrap"
            onChange={setFilter}
            options={FILTERS.map((option) => ({
              value: option.id,
              label: (
                <>
                  {option.label}
                  <span className="text-muted-foreground tabular ml-1.5 font-normal">
                    {counts[option.id].toLocaleString()}
                  </span>
                </>
              ),
            }))}
            value={filter}
          />
        </div>
      </div>

      {rows.length === 0 ? (
        <div className="text-muted-foreground rounded-xl border border-dashed px-6 py-8 text-[13.5px]">
          <p className="text-display text-foreground text-[17px]">No companies yet</p>
          <p className="mt-1 max-w-[56ch] leading-relaxed">
            Add one above and Foray will find its job board. To load the starter list of a few
            hundred tech companies, run <code className="text-foreground">pnpm seed:companies</code>.
          </p>
        </div>
      ) : visible.length === 0 ? (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-dashed px-5 py-4 text-[13.5px]">
          <p className="text-muted-foreground">
            {query.trim()
              ? `No companies match “${query.trim()}”${filter === "all" ? "" : ` under ${FILTERS.find((f) => f.id === filter)!.label}`}.`
              : NOTHING_IN[filter]}
          </p>
          <Button
            onClick={() => {
              setQuery("");
              setFilter("all");
            }}
            size="sm"
            variant="outline"
          >
            Show all companies
          </Button>
        </div>
      ) : (
        <div className="space-y-2">
          <div className="overflow-hidden rounded-xl border">
            <table className="w-full table-fixed text-[13.5px]">
              <thead className="bg-muted/40 text-muted-foreground border-b text-left text-[12.5px]">
                <tr>
                  <SortHeader
                    active={sort.key === "name"}
                    dir={sort.dir}
                    label="Company"
                    onClick={() => toggleSort("name")}
                  />
                  <th className="hidden w-[28%] py-2 pr-4 font-normal sm:table-cell" scope="col">
                    Board
                  </th>
                  <SortHeader
                    active={sort.key === "jobs"}
                    align="right"
                    className="w-[4.75rem] sm:w-[6.5rem]"
                    dir={sort.dir}
                    label={
                      <>
                        <span className="sm:hidden">Roles</span>
                        <span className="hidden sm:inline">Open roles</span>
                      </>
                    }
                    onClick={() => toggleSort("jobs")}
                  />
                  <SortHeader
                    active={sort.key === "fetched"}
                    className="hidden w-[9rem] md:table-cell"
                    dir={sort.dir}
                    label="Last fetched"
                    onClick={() => toggleSort("fetched")}
                  />
                  <th className="w-10 py-2 pr-2 sm:w-12 sm:pr-3" scope="col">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y">
                {visible.map((row) => (
                  <CompanyLine
                    busy={busy.has(row.slug)}
                    highlighted={highlight === row.slug}
                    key={row.slug}
                    onAction={(action) => void run(row, action)}
                    onHighlightShown={clearHighlight}
                    row={row}
                  />
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-muted-foreground tabular px-1 text-[12.5px]">
            {visible.length === rows.length
              ? `${rows.length.toLocaleString()} ${rows.length === 1 ? "company" : "companies"}`
              : `Showing ${visible.length.toLocaleString()} of ${rows.length.toLocaleString()} companies`}
          </p>
        </div>
      )}
    </div>
  );
}

function SortHeader({
  label,
  active,
  dir,
  onClick,
  align = "left",
  className,
}: {
  label: React.ReactNode;
  active: boolean;
  dir: "asc" | "desc";
  onClick: () => void;
  align?: "left" | "right";
  className?: string;
}) {
  const Arrow = dir === "asc" ? ArrowUp : ArrowDown;
  return (
    <th
      aria-sort={active ? (dir === "asc" ? "ascending" : "descending") : "none"}
      className={cn(
        "py-1 font-normal",
        align === "right" ? "pr-2 text-right sm:pr-4" : "pr-4 first:pl-3 sm:first:pl-4",
        className,
      )}
      scope="col"
    >
      <button
        className={cn(
          "hover:text-foreground focus-visible:ring-ring/50 -mx-1.5 inline-flex h-7 items-center gap-1 rounded-md px-1.5 outline-none focus-visible:ring-2",
          active && "text-foreground",
          align === "right" && "flex-row-reverse",
        )}
        onClick={onClick}
        type="button"
      >
        {label}
        <Arrow className={cn("size-3.5", active ? "opacity-80" : "opacity-0")} />
      </button>
    </th>
  );
}

function CompanyLine({
  row,
  busy,
  highlighted,
  onAction,
  onHighlightShown,
}: {
  row: CompanyRow;
  busy: boolean;
  highlighted: boolean;
  onAction: (action: "fetch" | "pause" | "resume") => void;
  onHighlightShown: () => void;
}) {
  const ref = useRef<HTMLTableRowElement>(null);

  // A company just added is scrolled to and briefly marked, so it is obvious
  // where it went in a list of hundreds.
  useEffect(() => {
    if (!highlighted) return;
    ref.current?.scrollIntoView({ block: "center", behavior: "smooth" });
    const timer = setTimeout(onHighlightShown, 2400);
    return () => clearTimeout(timer);
  }, [highlighted, onHighlightShown]);

  const url = boardUrl(row);
  const detail = boardDetail(row);
  const problem =
    row.lastFetchStatus && row.lastFetchStatus !== "ok"
      ? (FETCH_PROBLEM[row.lastFetchStatus] ?? row.lastFetchStatus)
      : null;
  const fetched = row.lastFetchedAt ? timeAgo(row.lastFetchedAt) : "Never";

  return (
    <tr
      className={cn(
        "transition-colors",
        highlighted ? "bg-accent" : "hover:bg-muted/40",
        busy && "opacity-60",
      )}
      ref={ref}
    >
      <td className="py-2.5 pr-2 pl-3 sm:pr-4 sm:pl-4">
        <div className="flex min-w-0 items-center gap-3">
          <CompanyMark name={row.name} size="sm" />
          <div className="min-w-0">
            <div className="flex min-w-0 items-center gap-2">
              <span
                className={cn(
                  "truncate font-medium",
                  row.status === "paused" && "text-muted-foreground",
                )}
              >
                {row.name}
              </span>
              {row.status !== "active" && (
                <span
                  className={cn(
                    "shrink-0 text-[12px]",
                    row.status === "invalid" ? "text-destructive" : "text-muted-foreground",
                  )}
                >
                  {row.status === "invalid" ? "Invalid" : "Paused"}
                </span>
              )}
            </div>
            {row.tags.length > 0 && (
              <p className="text-muted-foreground truncate text-[12.5px]">{row.tags.join(", ")}</p>
            )}
            {/* Narrow screens drop the board and fetch columns; say both here instead. */}
            <p className="text-muted-foreground truncate text-[12.5px] sm:hidden" suppressHydrationWarning>
              {BOARD_LABEL[row.sourceType]}, fetched {fetched === "Never" ? "never" : fetched}
            </p>
            {problem && (
              <p className="text-destructive truncate text-[12.5px] md:hidden">{problem}</p>
            )}
          </div>
        </div>
      </td>
      <td className="hidden py-2.5 pr-4 sm:table-cell">
        <div className="flex min-w-0 items-center gap-2">
          <span className="bg-secondary text-secondary-foreground inline-flex h-5 shrink-0 items-center rounded-full px-2 text-[11.5px] font-medium">
            {BOARD_LABEL[row.sourceType]}
          </span>
          {detail && (
            <span className="text-muted-foreground truncate text-[12.5px]" title={detail}>
              {detail}
            </span>
          )}
        </div>
      </td>
      <td
        className={cn(
          "tabular py-2.5 pr-2 text-right sm:pr-4",
          row.activeJobs === 0 && "text-muted-foreground",
        )}
      >
        {row.activeJobs.toLocaleString()}
      </td>
      <td className="hidden py-2.5 pr-4 md:table-cell">
        {/* Relative time and local date differ between server and browser; both are fine. */}
        <span
          className="text-muted-foreground block truncate text-[13px]"
          suppressHydrationWarning
          title={row.lastFetchedAt ? new Date(row.lastFetchedAt).toLocaleString() : undefined}
        >
          {fetched}
        </span>
        {problem && <span className="text-destructive block truncate text-[12.5px]">{problem}</span>}
      </td>
      <td className="py-2 pr-2 text-right sm:pr-3">
        {busy ? (
          <span className="inline-flex size-7 items-center justify-center">
            <Spinner className="text-muted-foreground" />
          </span>
        ) : (
          <DropdownMenu>
            <DropdownMenuTrigger
              aria-label={`Actions for ${row.name}`}
              className="text-muted-foreground hover:bg-muted hover:text-foreground data-popup-open:bg-muted focus-visible:ring-ring/50 inline-flex size-7 items-center justify-center rounded-md outline-none focus-visible:ring-2"
            >
              <Ellipsis className="size-4" />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-48">
              <DropdownMenuItem onClick={() => onAction("fetch")}>
                <RefreshCw />
                Fetch now
              </DropdownMenuItem>
              {row.status === "active" ? (
                <DropdownMenuItem onClick={() => onAction("pause")}>
                  <Pause />
                  Pause
                </DropdownMenuItem>
              ) : (
                <DropdownMenuItem onClick={() => onAction("resume")}>
                  <Play />
                  {row.status === "invalid" ? "Reactivate" : "Resume"}
                </DropdownMenuItem>
              )}
              {url && (
                <>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem
                    render={<a href={url} rel="noreferrer" target="_blank" />}
                  >
                    <ExternalLink />
                    Open job board
                  </DropdownMenuItem>
                </>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </td>
    </tr>
  );
}
