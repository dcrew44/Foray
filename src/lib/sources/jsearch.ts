import { env } from "@/env";
import { countRequest, requestsThisPeriod } from "@/lib/db/queries/usage";

import { cleanDescription } from "./clean";
import { contentHash, dedupeKey, jobId } from "./dedupe";
import { fetchText, HttpError } from "./http";
import { detectRemote, isPlausibleSalary, primaryLocation } from "./normalize";
import type {
  NormalizedJob,
  NormalizedSalary,
  RemoteKind,
} from "./types";

/**
 * JSearch — Google for Jobs, via RapidAPI.
 *
 * This is the only source that searches across companies rather than reading
 * one board, which is what stops the product from only ever finding jobs at
 * companies we already thought to register. It is also the only source that
 * costs money per call: the free tier is 200 requests a month and every result
 * page bills one, so the quota is checked before each request and counted after
 * — not estimated from a run's own tally, which would lose track the moment two
 * runs overlap.
 *
 * Endpoint note: the search path is `/search-v2` on the current (v5) API.
 * `/search` returns "Endpoint does not exist", and paging is by an opaque
 * `cursor` rather than a page number.
 */

const HOST = "jsearch.p.rapidapi.com";
const BASE = `https://${HOST}`;

/** The free tier. Set deliberately low so an accident cannot empty a month. */
const MONTHLY_BUDGET = Number(process.env.JSEARCH_MONTHLY_BUDGET ?? 180);

export class JSearchDisabledError extends Error {
  constructor(reason: string) {
    super(`JSearch unavailable: ${reason}`);
    this.name = "JSearchDisabledError";
  }
}

export class JSearchQuotaError extends Error {
  constructor(
    readonly used: number,
    readonly budget: number,
  ) {
    super(`JSearch monthly budget spent (${used}/${budget} requests)`);
    this.name = "JSearchQuotaError";
  }
}

type JSearchApplyOption = {
  apply_link?: string | null;
  is_direct?: boolean | null;
  publisher?: string | null;
};

export type JSearchJob = {
  job_id: string;
  job_title: string;
  employer_name?: string | null;
  employer_website?: string | null;
  job_publisher?: string | null;
  job_employment_type?: string | null;
  job_apply_link?: string | null;
  apply_options?: JSearchApplyOption[] | null;
  job_description?: string | null;
  job_is_remote?: boolean | null;
  job_posted_at_datetime_utc?: string | null;
  job_location?: string | null;
  job_city?: string | null;
  job_state?: string | null;
  job_country?: string | null;
  job_min_salary?: number | null;
  job_max_salary?: number | null;
  job_salary_period?: string | null;
  job_google_link?: string | null;
};

type JSearchResponse = {
  status?: string;
  data?: { jobs?: JSearchJob[] | null; cursor?: string | null } | null;
};

export type JSearchCriteria = {
  query: string;
  /** ISO-2, lowercase. JSearch defaults to `us` when omitted. */
  country?: string;
  datePosted?: "all" | "today" | "3days" | "week" | "month";
  remoteOnly?: boolean;
  /** Hard ceiling on requests for this call. Each page is one request. */
  maxRequests?: number;
};

/** JSearch reports the period as a word; ours is an enum. */
function periodOf(value: string | null | undefined): NormalizedSalary["period"] {
  const period = value?.toLowerCase() ?? "";
  if (period.includes("hour")) return "hour";
  if (period.includes("month")) return "month";
  if (period.includes("year") || period.includes("annum")) return "year";
  return null;
}

/**
 * A company slug we can register, if an apply link points at a board we read.
 *
 * This is the quiet payoff of the aggregator: every result that applies through
 * Greenhouse, Lever or Ashby tells us about a board we could be reading
 * directly — for free, at full fidelity, without spending a request.
 */
export function atsFromApplyOptions(
  job: JSearchJob,
): { source: "greenhouse" | "lever" | "ashby"; slug: string } | null {
  const links = [
    job.job_apply_link,
    ...(job.apply_options ?? []).map((option) => option.apply_link),
  ].filter((link): link is string => Boolean(link));

  for (const link of links) {
    const greenhouse = /(?:boards|job-boards)\.greenhouse\.io\/([a-z0-9_-]+)/i.exec(
      link,
    );
    if (greenhouse) {
      return { source: "greenhouse", slug: greenhouse[1].toLowerCase() };
    }

    const lever = /jobs\.lever\.co\/([a-z0-9_-]+)/i.exec(link);
    if (lever) return { source: "lever", slug: lever[1].toLowerCase() };

    const ashby = /jobs\.ashbyhq\.com\/([a-z0-9_-]+)/i.exec(link);
    if (ashby) return { source: "ashby", slug: ashby[1].toLowerCase() };
  }

  return null;
}

/** A stable company slug from the employer name — aggregator rows have none. */
export function employerSlug(name: string): string {
  return (
    name
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-|-$/g, "")
      .slice(0, 60) || "unknown"
  );
}

export function normalizeJSearchJob(job: JSearchJob): NormalizedJob {
  const companyName = job.employer_name?.trim() || "Unknown";
  const companySlug = employerSlug(companyName);

  const locations = [
    job.job_location ??
      [job.job_city, job.job_state, job.job_country]
        .filter(Boolean)
        .join(", "),
  ].filter(Boolean) as string[];

  const location = primaryLocation(locations);

  // Already plain text — JSearch does not send HTML — but it still goes
  // through cleaning so trailing boilerplate is stripped the same way.
  const descriptionRaw = job.job_description ?? null;
  const cleaned = cleanDescription(descriptionRaw);

  const remote: RemoteKind =
    job.job_is_remote === true
      ? "remote"
      : detectRemote({
          location: locations.join(" "),
          title: job.job_title,
          description: cleaned.text,
        });

  const reported: NormalizedSalary = {
    min: job.job_min_salary ?? null,
    max: job.job_max_salary ?? null,
    currency: "USD",
    period: periodOf(job.job_salary_period),
  };
  const salary = isPlausibleSalary(reported)
    ? reported
    : { min: null, max: null, currency: null, period: null };

  const applyLink =
    (job.apply_options ?? []).find((option) => option.is_direct)?.apply_link ??
    job.job_apply_link ??
    null;

  return {
    id: jobId("jsearch", companySlug, job.job_id),
    source: "jsearch",
    externalId: job.job_id,
    companySlug,
    companyName,
    title: job.job_title,
    locations,
    locationNorm: location,
    remote,
    employmentType: job.job_employment_type ?? null,
    salary,
    descriptionRaw,
    descriptionText: cleaned.text || null,
    url: job.job_google_link ?? applyLink ?? job.job_apply_link ?? "",
    applyUrl: applyLink,
    postedAt: job.job_posted_at_datetime_utc
      ? new Date(job.job_posted_at_datetime_utc)
      : null,
    raw: job,
    contentHash: contentHash({
      title: job.job_title,
      locations,
      descriptionRaw,
      salaryMin: salary.min,
      salaryMax: salary.max,
    }),
    dedupeKey: dedupeKey({
      companyName,
      title: job.job_title,
      primaryLocation: location?.city ?? location?.raw ?? null,
    }),
  };
}

export type JSearchResult = {
  jobs: NormalizedJob[];
  /** Boards worth registering, found in apply links. */
  discoveredBoards: Array<{
    source: "greenhouse" | "lever" | "ashby";
    slug: string;
    companyName: string;
  }>;
  requestsUsed: number;
  /** Total spent this calendar month, after this call. */
  requestsThisMonth: number;
};

export async function searchJSearch(
  criteria: JSearchCriteria,
): Promise<JSearchResult> {
  if (!env.JSEARCH_ENABLED) {
    throw new JSearchDisabledError("JSEARCH_ENABLED is false");
  }
  if (!env.RAPIDAPI_KEY) {
    throw new JSearchDisabledError("RAPIDAPI_KEY is not set");
  }

  const budgetBefore = await requestsThisPeriod("jsearch");
  if (budgetBefore >= MONTHLY_BUDGET) {
    throw new JSearchQuotaError(budgetBefore, MONTHLY_BUDGET);
  }

  const maxRequests = Math.max(
    1,
    Math.min(criteria.maxRequests ?? 1, MONTHLY_BUDGET - budgetBefore),
  );

  const jobs: NormalizedJob[] = [];
  const boards = new Map<string, JSearchResult["discoveredBoards"][number]>();

  let cursor: string | null = null;
  let used = 0;
  let requestsThisMonth = budgetBefore;

  for (let page = 0; page < maxRequests; page += 1) {
    const url = new URL(`${BASE}/search-v2`);
    url.searchParams.set("query", criteria.query);
    url.searchParams.set("country", criteria.country ?? "us");
    url.searchParams.set("date_posted", criteria.datePosted ?? "month");
    if (criteria.remoteOnly) url.searchParams.set("work_from_home", "true");
    if (cursor) url.searchParams.set("cursor", cursor);

    // Counted before the response is read: a request that was sent and then
    // failed still spent quota.
    requestsThisMonth = await countRequest("jsearch");
    used += 1;

    let body: string;
    try {
      body = await fetchText(url.toString(), {
        timeoutMs: 45_000,
        retries: 1,
        accept: "application/json",
        headers: {
          "x-rapidapi-key": env.RAPIDAPI_KEY,
          "x-rapidapi-host": HOST,
        },
      });
    } catch (error) {
      // A failure mid-paging still returns what earlier pages produced; the
      // caller gets fewer jobs rather than none.
      if (error instanceof HttpError && jobs.length > 0) break;
      throw error;
    }

    const response = JSON.parse(body) as JSearchResponse;
    const page_jobs = response.data?.jobs ?? [];

    for (const job of page_jobs) {
      jobs.push(normalizeJSearchJob(job));

      const board = atsFromApplyOptions(job);
      if (board) {
        const key = `${board.source}:${board.slug}`;
        if (!boards.has(key)) {
          boards.set(key, {
            ...board,
            companyName: job.employer_name?.trim() || board.slug,
          });
        }
      }
    }

    cursor = response.data?.cursor ?? null;
    if (!cursor || page_jobs.length === 0) break;
  }

  return {
    jobs,
    discoveredBoards: [...boards.values()],
    requestsUsed: used,
    requestsThisMonth,
  };
}

/** For the settings page and the run budget. */
export async function jsearchBudget() {
  const used = await requestsThisPeriod("jsearch");
  return {
    used,
    budget: MONTHLY_BUDGET,
    remaining: Math.max(MONTHLY_BUDGET - used, 0),
    enabled: env.JSEARCH_ENABLED && Boolean(env.RAPIDAPI_KEY),
  };
}
