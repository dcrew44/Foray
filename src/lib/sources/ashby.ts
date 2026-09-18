import { fetchText, HttpError } from "./http";
import { cleanDescription } from "./clean";
import { contentHash, dedupeKey, jobId } from "./dedupe";
import {
  detectRemote,
  isPlausibleSalary,
  parseSalary,
  primaryLocation,
} from "./normalize";
import {
  BoardNotFoundError,
  type FetchBoardResult,
  type JobSourceAdapter,
  type NormalizedJob,
  type NormalizedSalary,
  type RemoteKind,
} from "./types";

/**
 * Ashby job boards.
 *
 * The richest of the three: a complete description in one field, an explicit
 * remote boolean, and structured compensation — when the board chooses to
 * publish it, which many do not.
 */

type AshbyComponent = {
  compensationType?: string | null;
  interval?: string | null;
  currencyCode?: string | null;
  minValue?: number | null;
  maxValue?: number | null;
};

type AshbyJob = {
  id: string;
  title: string;
  department?: string | null;
  team?: string | null;
  employmentType?: string | null;
  location?: string | null;
  secondaryLocations?: Array<{ location?: string | null }> | null;
  isRemote?: boolean | null;
  workplaceType?: string | null;
  isListed?: boolean | null;
  publishedAt?: string | null;
  jobUrl: string;
  applyUrl?: string | null;
  descriptionHtml?: string | null;
  descriptionPlain?: string | null;
  compensation?: {
    summaryComponents?: AshbyComponent[] | null;
    scrapeableCompensationSalarySummary?: string | null;
  } | null;
};

type AshbyBoard = { jobs?: AshbyJob[] | null };

const BASE = "https://api.ashbyhq.com/posting-api/job-board";

const WORKPLACE: Record<string, RemoteKind> = {
  remote: "remote",
  hybrid: "hybrid",
  onsite: "onsite",
};

/** Ashby writes the interval as a phrase: "1 YEAR", "1 HOUR". */
function periodFromInterval(
  interval: string | null | undefined,
): NormalizedSalary["period"] {
  const value = interval?.toLowerCase() ?? "";
  if (value.includes("hour")) return "hour";
  if (value.includes("month")) return "month";
  if (value.includes("year")) return "year";
  return null;
}

/** Equity components carry null amounts; only cash salary is a salary. */
function salaryFrom(job: AshbyJob): NormalizedSalary | null {
  const component = (job.compensation?.summaryComponents ?? []).find(
    (item) =>
      item.compensationType?.toLowerCase() === "salary" &&
      typeof item.minValue === "number",
  );
  if (!component) return null;

  const salary: NormalizedSalary = {
    min: component.minValue ?? null,
    max: component.maxValue ?? null,
    currency: component.currencyCode ?? null,
    period: periodFromInterval(component.interval),
  };

  // Structured does not mean correct — see isPlausibleSalary.
  return isPlausibleSalary(salary) ? salary : null;
}

export function normalizeAshbyJob(slug: string, job: AshbyJob): NormalizedJob {
  const locations = [
    job.location,
    ...(job.secondaryLocations ?? []).map((item) => item.location),
  ]
    .map((value) => value?.trim())
    .filter((value): value is string => Boolean(value));

  const location = primaryLocation(locations);
  const descriptionRaw = job.descriptionHtml ?? job.descriptionPlain ?? null;
  const cleaned = cleanDescription(descriptionRaw);

  const remote =
    WORKPLACE[job.workplaceType?.toLowerCase() ?? ""] ??
    detectRemote({
      location: locations.join(" "),
      title: job.title,
      isRemoteFlag: job.isRemote ?? null,
      description: cleaned.text,
    });

  const salary =
    salaryFrom(job) ??
    parseSalary(
      job.compensation?.scrapeableCompensationSalarySummary ?? cleaned.text,
    );

  return {
    id: jobId("ashby", slug, job.id),
    source: "ashby",
    externalId: job.id,
    companySlug: slug,
    companyName: slug,
    title: job.title,
    locations,
    locationNorm: location,
    remote,
    employmentType: job.employmentType ?? null,
    salary,
    descriptionRaw,
    descriptionText: cleaned.text || null,
    url: job.jobUrl,
    applyUrl: job.applyUrl ?? null,
    postedAt: job.publishedAt ? new Date(job.publishedAt) : null,
    raw: job,
    contentHash: contentHash({
      title: job.title,
      locations,
      descriptionRaw,
      salaryMin: salary.min,
      salaryMax: salary.max,
    }),
    dedupeKey: dedupeKey({
      companyName: slug,
      title: job.title,
      primaryLocation: location?.city ?? location?.raw ?? null,
    }),
  };
}

export const ashby: JobSourceAdapter = {
  id: "ashby",

  async fetchBoard(slug: string): Promise<FetchBoardResult> {
    const url = `${BASE}/${encodeURIComponent(slug)}?includeCompensation=true`;

    let body: string;
    try {
      body = await fetchText(url);
    } catch (error) {
      // Ashby answers an unknown slug with plain text "Not Found", so the
      // status has to be checked before anything tries to parse the body.
      if (error instanceof HttpError && error.status === 404) {
        throw new BoardNotFoundError("ashby", slug);
      }
      throw error;
    }

    const board = JSON.parse(body) as AshbyBoard;
    return {
      companySlug: slug,
      // Unlisted postings are not public; a board can also be real but empty.
      jobs: (board.jobs ?? [])
        .filter((job) => job.isListed !== false)
        .map((job) => normalizeAshbyJob(slug, job)),
    };
  },
};
