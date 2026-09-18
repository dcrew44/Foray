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
 * Lever job boards.
 *
 * The trap here is that `description` is not the description: it is the opening
 * plus the body, and the requirements live in `lists[]`, which it does not
 * include. On a real posting that is several thousand characters of exactly the
 * content a match is scored against, so the full posting has to be reassembled.
 */

type LeverList = { text?: string | null; content?: string | null };

type LeverPosting = {
  id: string;
  text: string;
  createdAt?: number | null;
  country?: string | null;
  workplaceType?: string | null;
  hostedUrl: string;
  applyUrl?: string | null;
  categories?: {
    commitment?: string | null;
    department?: string | null;
    location?: string | null;
    team?: string | null;
    allLocations?: string[] | null;
  } | null;
  lists?: LeverList[] | null;
  description?: string | null;
  additional?: string | null;
  salaryRange?: {
    min?: number | null;
    max?: number | null;
    currency?: string | null;
    interval?: string | null;
  } | null;
};

const BASE = "https://api.lever.co/v0/postings";

/** `description` omits `lists[]`, so the posting is stitched back together. */
function fullDescription(posting: LeverPosting): string | null {
  const parts: string[] = [];
  if (posting.description) parts.push(posting.description);

  for (const list of posting.lists ?? []) {
    if (list.text) parts.push(`<h3>${list.text}</h3>`);
    if (list.content) parts.push(list.content);
  }

  if (posting.additional) parts.push(posting.additional);
  return parts.length ? parts.join("\n") : null;
}

const WORKPLACE: Record<string, RemoteKind> = {
  remote: "remote",
  hybrid: "hybrid",
  onsite: "onsite",
  "on-site": "onsite",
};

/** `per-year-salary`, `per-hour-wage`, `per-month-salary`. */
function periodFromInterval(
  interval: string | null | undefined,
): NormalizedSalary["period"] {
  if (!interval) return null;
  if (interval.includes("hour")) return "hour";
  if (interval.includes("month")) return "month";
  if (interval.includes("year")) return "year";
  return null;
}

export function normalizeLeverJob(
  slug: string,
  posting: LeverPosting,
): NormalizedJob {
  const categories = posting.categories ?? {};
  const locations =
    categories.allLocations?.filter(Boolean) ??
    (categories.location ? [categories.location] : []);

  const location = primaryLocation(locations);
  const descriptionRaw = fullDescription(posting);
  const cleaned = cleanDescription(descriptionRaw);

  const remote =
    WORKPLACE[posting.workplaceType?.toLowerCase() ?? ""] ??
    detectRemote({
      location: locations.join(" "),
      title: posting.text,
      description: cleaned.text,
    });

  // Lever is the one board here with a clean numeric range, so it is preferred
  // over anything the description happens to say — but still only when the
  // numbers are plausible for the interval they claim.
  const structured: NormalizedSalary | null = posting.salaryRange?.min
    ? {
        min: posting.salaryRange.min ?? null,
        max: posting.salaryRange.max ?? null,
        currency: posting.salaryRange.currency ?? null,
        period: periodFromInterval(posting.salaryRange.interval),
      }
    : null;

  const salary =
    structured && isPlausibleSalary(structured)
      ? structured
      : parseSalary(cleaned.text);

  const companyName = slug;

  return {
    id: jobId("lever", slug, posting.id),
    source: "lever",
    externalId: posting.id,
    companySlug: slug,
    companyName,
    title: posting.text,
    locations,
    locationNorm: location,
    remote,
    employmentType: categories.commitment ?? null,
    salary,
    descriptionRaw,
    descriptionText: cleaned.text || null,
    url: posting.hostedUrl,
    // Lever's apply form is a separate page from the posting.
    applyUrl: posting.applyUrl ?? null,
    postedAt: posting.createdAt ? new Date(posting.createdAt) : null,
    raw: posting,
    contentHash: contentHash({
      title: posting.text,
      locations,
      descriptionRaw,
      salaryMin: salary.min,
      salaryMax: salary.max,
    }),
    dedupeKey: dedupeKey({
      companyName,
      title: posting.text,
      primaryLocation: location?.city ?? location?.raw ?? null,
    }),
  };
}

export const lever: JobSourceAdapter = {
  id: "lever",

  async fetchBoard(slug: string): Promise<FetchBoardResult> {
    const url = `${BASE}/${encodeURIComponent(slug)}?mode=json`;

    let body: string;
    try {
      body = await fetchText(url);
    } catch (error) {
      if (error instanceof HttpError && error.status === 404) {
        throw new BoardNotFoundError("lever", slug);
      }
      throw error;
    }

    // A real board with nothing open returns `[]`, which is not a 404.
    const postings = JSON.parse(body) as LeverPosting[];
    return {
      companySlug: slug,
      jobs: (Array.isArray(postings) ? postings : []).map((posting) =>
        normalizeLeverJob(slug, posting),
      ),
    };
  },
};
