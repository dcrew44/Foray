import type { Job } from "pg-boss";

import { discoverCompany } from "@/lib/companies/discovery";
import { refreshCompanies, staleCutoff } from "@/lib/companies/refresh";
import { listCompanies, staleCompanies } from "@/lib/db/queries/companies";
import { emit } from "@/lib/runs/events";
import type {
  CompanyDiscoverPayload,
  CompanyRefreshPayload,
} from "@/lib/queue/queues";

/**
 * Company discovery and board refresh, off the request path.
 *
 * Discovery makes several HTTP calls and can take tens of seconds; refresh
 * touches every registered board. Neither belongs in a Next request.
 */

export async function handleCompanyDiscover(
  jobs: Job<CompanyDiscoverPayload>[],
) {
  for (const job of jobs) {
    const { userId, names, runId } = job.data;

    for (const name of names) {
      const result = await discoverCompany({
        nameOrUrl: name,
        addedByUserId: userId,
        discoveredVia: "agent",
      });

      await emit({
        userId,
        runId: runId ?? null,
        kind: result.registered ? "company.discovered" : "company.not-found",
        level: result.registered ? "info" : "warn",
        message: result.registered
          ? `Registered ${result.registered.name} (${result.source}${result.slug ? `:${result.slug}` : ""})`
          : `Could not find a job board for "${name}"`,
        data: { trail: result.trail },
      });
    }
  }
}

export async function handleCompanyRefresh(jobs: Job<CompanyRefreshPayload>[]) {
  for (const job of jobs) {
    // The cron sends no payload at all, so `data` is null there rather than
    // an empty object — destructuring it directly crashed every scheduled run.
    const { userId, runId, all } = job.data ?? {};

    const targets = all
      ? await listCompanies({ status: "active" })
      : await staleCompanies(staleCutoff());

    if (targets.length === 0) {
      console.log("[worker] company.refresh: nothing stale");
      continue;
    }

    console.log(`[worker] company.refresh: ${targets.length} boards`);
    const started = Date.now();
    const outcomes = await refreshCompanies(targets);

    const ok = outcomes.filter((o) => o.status === "ok");
    const seen = ok.reduce((total, o) => total + o.jobs, 0);
    const deactivated = ok.reduce(
      (total, o) => total + (o.ingest?.deactivated ?? 0),
      0,
    );

    const message =
      `Refreshed ${ok.length}/${targets.length} boards — ${seen} postings, ` +
      `${deactivated} closed`;

    console.log(`[worker] ${message} in ${((Date.now() - started) / 1000).toFixed(1)}s`);

    if (userId) {
      await emit({
        userId,
        runId: runId ?? null,
        kind: "company.refresh.done",
        message,
        data: {
          boards: targets.length,
          ok: ok.length,
          notFound: outcomes.filter((o) => o.status === "not-found").length,
          errored: outcomes.filter((o) => o.status === "error").length,
        },
      });
    }
  }
}
