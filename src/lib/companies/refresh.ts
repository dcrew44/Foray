import { db } from "@/lib/db";
import { recordFetch } from "@/lib/db/queries/companies";
import { companies } from "@/lib/db/schema";
import { fetchCareersPage } from "@/lib/sources/careers-page";
import { eq } from "drizzle-orm";
import type { Company } from "@/lib/db/schema";
import { BOARD_ADAPTERS, type BoardSourceId } from "@/lib/sources";
import { ingestJobs, type IngestResult } from "@/lib/sources/ingest";
import { BoardNotFoundError } from "@/lib/sources/types";

/**
 * Re-fetching one company's board into the mirror.
 *
 * A 404 here is meaningful in a way a network error is not: the board is gone,
 * so the company is marked invalid and stops being retried every six hours. A
 * transient failure just records the failure and leaves the company active.
 */

export type RefreshOutcome = {
  company: string;
  status: "ok" | "not-found" | "unsupported" | "error";
  jobs: number;
  ingest?: IngestResult;
  error?: string;
};

export async function refreshCompany(company: Company): Promise<RefreshOutcome> {
  const adapter =
    company.sourceType in BOARD_ADAPTERS
      ? BOARD_ADAPTERS[company.sourceType as BoardSourceId]
      : null;

  // No API for this company: read its careers pages with a model instead.
  if (company.sourceType === "careers_page") {
    return refreshCareersPage(company);
  }

  if (!adapter?.fetchBoard || !company.atsSlug) {
    return { company: company.name, status: "unsupported", jobs: 0 };
  }

  try {
    const board = await adapter.fetchBoard(company.atsSlug);

    const ingest = await ingestJobs({
      company,
      jobs: board.jobs,
      // A full board fetch is the only place we can safely conclude that a
      // posting we are not seeing has actually been taken down.
      deactivateMissing: true,
    });

    await recordFetch({
      companyId: company.id,
      status: "ok",
      jobCount: board.jobs.length,
    });

    return {
      company: company.name,
      status: "ok",
      jobs: board.jobs.length,
      ingest,
    };
  } catch (error) {
    const gone = error instanceof BoardNotFoundError;

    await recordFetch({
      companyId: company.id,
      status: gone ? "not-found" : "error",
      jobCount: 0,
      // Only a 404 is conclusive. A timeout should not retire a company.
      markInvalid: gone,
    });

    return {
      company: company.name,
      status: gone ? "not-found" : "error",
      jobs: 0,
      error: error instanceof Error ? error.message : String(error),
    };
  }
}

/** Runs refreshes with a small pool — we are a guest on these APIs. */
export async function refreshCompanies(
  companies: Company[],
  concurrency = 6,
): Promise<RefreshOutcome[]> {
  const results: RefreshOutcome[] = new Array(companies.length);
  let cursor = 0;

  await Promise.all(
    Array.from({ length: Math.min(concurrency, companies.length) }, async () => {
      for (;;) {
        const index = cursor++;
        if (index >= companies.length) return;
        results[index] = await refreshCompany(companies[index]);
      }
    }),
  );

  return results;
}

/**
 * The careers-page path.
 *
 * Deliberately does not deactivate missing postings: an extraction that reads
 * fewer roles than last time is more likely to be a page that changed shape
 * than a company that closed half its jobs. Board fetches are authoritative
 * about disappearance; this is not.
 */
async function refreshCareersPage(company: Company): Promise<RefreshOutcome> {
  if (company.careersUrls.length === 0) {
    return { company: company.name, status: "unsupported", jobs: 0 };
  }

  try {
    const result = await fetchCareersPage(company, {
      context: { userId: company.addedByUserId },
    });

    const ingest = await ingestJobs({ company, jobs: result.jobs });

    await recordFetch({
      companyId: company.id,
      status: result.needsBrowser ? "needs-browser" : "ok",
      jobCount: result.jobs.length,
    });

    // The flag is set *and cleared* here. A page that failed once and then
    // extracted fine — because the site changed, or because the extractor did
    // — must stop being routed to Phase 2's browser, which is expensive and
    // not yet built.
    if (result.needsBrowser !== company.needsBrowser) {
      await db
        .update(companies)
        .set({ needsBrowser: result.needsBrowser, updatedAt: new Date() })
        .where(eq(companies.id, company.id));
    }

    return {
      company: company.name,
      status: "ok",
      jobs: result.jobs.length,
      ingest,
    };
  } catch (error) {
    await recordFetch({
      companyId: company.id,
      status: "error",
      jobCount: 0,
    });
    return {
      company: company.name,
      status: "error",
      jobs: 0,
      error: error instanceof Error ? error.message : String(error),
    };
  }
}
