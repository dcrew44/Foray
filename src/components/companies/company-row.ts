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
  workable: "Workable",
  recruitee: "Recruitee",
  gem: "Gem",
  jibe: "iCIMS",
  workday: "Workday",
  smartrecruiters: "SmartRecruiters",
  icims: "iCIMS",
  careers_page: "Careers page",
};

/** Where a posting came from, in words, for the match page. */
export const SOURCE_LABEL: Record<string, string> = {
  greenhouse: "Greenhouse board",
  lever: "Lever board",
  ashby: "Ashby board",
  workable: "Workable board",
  recruitee: "Recruitee board",
  gem: "Gem board",
  jibe: "iCIMS career site",
  workday: "Workday career site",
  smartrecruiters: "SmartRecruiters board",
  icims: "iCIMS portal",
  jsearch: "Job aggregator",
  web: "Careers page",
};

/** What a failed fetch means, in words. `ok` has no entry. */
export const FETCH_PROBLEM: Record<string, string> = {
  "not-found": "Board not found",
  error: "Last fetch failed",
  unsupported: "Cannot be fetched",
  "needs-browser": "Needs a browser",
};
