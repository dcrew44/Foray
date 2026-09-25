import type { Company } from "@/lib/db/schema";

/**
 * One company as the /companies table shows it. Shared by the page, the
 * server actions that return an updated row, and the client table — so it is
 * plain data with no server imports beyond a type.
 */
export type CompanyRow = {
  slug: string;
  name: string;
  domain: string | null;
  sourceType: Company["sourceType"];
  atsSlug: string | null;
  /** The first careers page, for companies read without an ATS API. */
  careersUrl: string | null;
  status: Company["status"];
  needsBrowser: boolean;
  tags: string[];
  lastFetchedAt: string | null;
  lastFetchStatus: string | null;
  lastFetchJobCount: number | null;
  discoveredVia: Company["discoveredVia"];
  /** Open postings in the mirror right now. */
  activeJobs: number;
};

export function toCompanyRow(company: Company, activeJobs: number): CompanyRow {
  return {
    slug: company.slug,
    name: company.name,
    domain: company.domain,
    sourceType: company.sourceType,
    atsSlug: company.atsSlug,
    careersUrl: company.careersUrls[0] ?? null,
    status: company.status,
    needsBrowser: company.needsBrowser,
    tags: company.tags,
    lastFetchedAt: company.lastFetchedAt?.toISOString() ?? null,
    lastFetchStatus: company.lastFetchStatus,
    lastFetchJobCount: company.lastFetchJobCount,
    discoveredVia: company.discoveredVia,
    activeJobs,
  };
}

export const BOARD_LABEL: Record<Company["sourceType"], string> = {
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
