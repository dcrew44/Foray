import type { CompanyRow } from "@/lib/api/types";
import type { Company } from "@/lib/db/schema";

/**
 * One company as the /companies table shows it is Django's `CompanyRow` (the companies page and
 * a status change both answer with it). This module keeps the labels the table shows.
 */
export type { CompanyRow };

/**
 * Legacy: only `src/app/actions/companies.ts` still calls this, and nothing calls that action
 * any more. Step 11 deletes both, together with the `Company` import above and the `none`
 * board label below (Django has no board-less source type).
 */
export function toCompanyRow(company: Company, activeJobs: number): CompanyRow {
  return {
    slug: company.slug,
    name: company.name,
    domain: company.domain,
    sourceType: company.sourceType as CompanyRow["sourceType"],
    atsSlug: company.atsSlug,
    careersUrl: company.careersUrls[0] ?? null,
    status: company.status,
    needsBrowser: company.needsBrowser,
    tags: company.tags,
    lastFetchedAt: company.lastFetchedAt?.toISOString() ?? null,
    lastFetchStatus: company.lastFetchStatus as CompanyRow["lastFetchStatus"],
    lastFetchJobCount: company.lastFetchJobCount,
    discoveredVia: company.discoveredVia,
    activeJobs,
  };
}

export const BOARD_LABEL: Record<CompanyRow["sourceType"] | "none", string> = {
  greenhouse: "Greenhouse",
  lever: "Lever",
  ashby: "Ashby",
  careers_page: "Careers page",
  none: "No board",
};

/** What a failed fetch means, in words. `ok` has no entry. */
export const FETCH_PROBLEM: Record<string, string> = {
  "not-found": "Board not found",
  error: "Last fetch failed",
  unsupported: "Cannot be fetched",
  "needs-browser": "Needs a browser",
};
