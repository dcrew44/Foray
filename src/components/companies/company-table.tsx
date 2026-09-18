"use client";

import { useState, useTransition } from "react";

import {
  addCompanyAction,
  refreshAllAction,
  refreshCompanyAction,
  setStatusAction,
} from "@/app/actions/companies";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

export type CompanyRow = {
  slug: string;
  name: string;
  sourceType: string;
  atsSlug: string | null;
  status: string;
  tags: string[];
  lastFetchedAt: string | null;
  lastFetchStatus: string | null;
  lastFetchJobCount: number | null;
  discoveredVia: string;
  activeJobs: number;
};

export function CompanyTable({ companies }: { companies: CompanyRow[] }) {
  const [pending, startTransition] = useTransition();
  const [query, setQuery] = useState("");
  const [nameOrUrl, setNameOrUrl] = useState("");
  const [notice, setNotice] = useState<string | null>(null);

  const visible = query
    ? companies.filter((company) =>
        `${company.name} ${company.atsSlug ?? ""} ${company.tags.join(" ")}`
          .toLowerCase()
          .includes(query.toLowerCase()),
      )
    : companies;

  const run = (action: () => Promise<{ message: string }>) =>
    startTransition(async () => {
      setNotice(null);
      const result = await action();
      setNotice(result.message);
    });

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-2">
        <Input
          className="max-w-xs"
          onChange={(event) => setNameOrUrl(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && nameOrUrl.trim()) {
              run(() => addCompanyAction(nameOrUrl.trim()));
              setNameOrUrl("");
            }
          }}
          placeholder="Add a company, domain or board URL"
          value={nameOrUrl}
        />
        <Button
          disabled={pending || !nameOrUrl.trim()}
          onClick={() => {
            run(() => addCompanyAction(nameOrUrl.trim()));
            setNameOrUrl("");
          }}
        >
          {pending ? "Working…" : "Add"}
        </Button>
        <Button
          disabled={pending}
          onClick={() => run(refreshAllAction)}
          variant="outline"
        >
          Refresh stale boards
        </Button>
        <Input
          className="ml-auto max-w-xs"
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Filter…"
          value={query}
        />
      </div>

      {notice && (
        <p className="text-muted-foreground text-sm" role="status">
          {notice}
        </p>
      )}

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-muted-foreground border-b text-left text-xs uppercase">
            <tr>
              <th className="py-2 pr-4 font-medium">Company</th>
              <th className="py-2 pr-4 font-medium">Board</th>
              <th className="py-2 pr-4 font-medium">Status</th>
              <th className="py-2 pr-4 text-right font-medium">Open jobs</th>
              <th className="py-2 pr-4 font-medium">Last fetch</th>
              <th className="py-2 font-medium" />
            </tr>
          </thead>
          <tbody className="divide-border divide-y">
            {visible.map((company) => (
              <tr key={company.slug}>
                <td className="py-2 pr-4">
                  <div className="font-medium">{company.name}</div>
                  {company.tags.length > 0 && (
                    <div className="text-muted-foreground text-xs">
                      {company.tags.join(" · ")}
                    </div>
                  )}
                </td>
                <td className="text-muted-foreground py-2 pr-4">
                  {company.sourceType}
                  {company.atsSlug ? `:${company.atsSlug}` : ""}
                </td>
                <td className="py-2 pr-4">
                  <StatusBadge status={company.status} />
                </td>
                <td className="py-2 pr-4 text-right tabular-nums">
                  {company.activeJobs}
                </td>
                <td className="text-muted-foreground py-2 pr-4 whitespace-nowrap">
                  {company.lastFetchedAt
                    ? new Date(company.lastFetchedAt).toLocaleString()
                    : "never"}
                  {company.lastFetchStatus &&
                    company.lastFetchStatus !== "ok" && (
                      <span className="text-destructive ml-2">
                        {company.lastFetchStatus}
                      </span>
                    )}
                </td>
                <td className="py-2 text-right whitespace-nowrap">
                  <Button
                    disabled={pending}
                    onClick={() => run(() => refreshCompanyAction(company.slug))}
                    size="sm"
                    variant="ghost"
                  >
                    Re-fetch
                  </Button>
                  <Button
                    disabled={pending}
                    onClick={() =>
                      run(() =>
                        setStatusAction(
                          company.slug,
                          company.status === "paused" ? "active" : "paused",
                        ),
                      )
                    }
                    size="sm"
                    variant="ghost"
                  >
                    {company.status === "paused" ? "Resume" : "Pause"}
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {visible.length === 0 && (
        <p className="text-muted-foreground text-sm">
          {companies.length === 0
            ? "No companies yet. Add one above, or run pnpm seed:companies."
            : "Nothing matches that filter."}
        </p>
      )}
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const tone =
    status === "active"
      ? "text-foreground"
      : status === "invalid"
        ? "text-destructive"
        : "text-muted-foreground";
  return <span className={`text-xs uppercase ${tone}`}>{status}</span>;
}
