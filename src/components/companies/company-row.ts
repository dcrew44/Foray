import type { CompanyRow } from "@/lib/api/types";

/**
 * One company as the /companies table shows it is Django's `CompanyRow` (the companies page and
 * a status change both answer with it). This module keeps the labels the table shows.
 */
export type { CompanyRow };

export const BOARD_LABEL: Record<CompanyRow["sourceType"], string> = {
  greenhouse: "Greenhouse",
  lever: "Lever",
  ashby: "Ashby",
  careers_page: "Careers page",
};

/** What a failed fetch means, in words. `ok` has no entry. */
export const FETCH_PROBLEM: Record<string, string> = {
  "not-found": "Board not found",
  error: "Last fetch failed",
  unsupported: "Cannot be fetched",
  "needs-browser": "Needs a browser",
};
