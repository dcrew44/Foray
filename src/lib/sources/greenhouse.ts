import { fetchText, HttpError } from "./http";
import { cleanDescription } from "./clean";
import { contentHash, dedupeKey, jobId } from "./dedupe";
import {
  detectRemote,
  parseSalary,
  primaryLocation,
} from "./normalize";
import {
  BoardNotFoundError,
  type FetchBoardResult,
  type JobSourceAdapter,
  type NormalizedJob,
} from "./types";

/**
 * Greenhouse job boards.
 *
 * Two quirks shape this adapter. Its `content` is entity-escaped HTML, handled
 * by clean.ts. And it has no compensation fields at all — pay lives inside a
 * `pay-range` block in the description, so the salary here is parsed out of the
 * cleaned text rather than read from a column.
 */

type GreenhouseJob = {
  id: number;
  title: string;
  company_name?: string | null;
  absolute_url: string;
  content?: string | null;
  updated_at?: string | null;
  first_published?: string | null;
  location?: { name?: string | null } | null;
  offices?: Array<{ name?: string | null; location?: string | null }> | null;
  metadata?: Array<{
    name?: string | null;
    value?: string | string[] | null;
  }> | null;
};

type GreenhouseBoard = { jobs?: GreenhouseJob[] | null };

const BASE = "https://boards-api.greenhouse.io/v1/boards";

/**
 * Greenhouse packs several cities into one free-text string, and it is not
 * consistent about the separator: real boards use both `|` and `;`, sometimes
 * in the same value ("New York City, NY; San Francisco, CA | New York City,
 * NY"), and repeat themselves. Splitting on only one of them leaves a
 * compound string that no location parser can read, which quietly drops the
 * job out of every metro filter.
 */
function splitLocations(job: GreenhouseJob): string[] {
  const named = job.location?.name
    ?.split(/[|;]/)
    .map((part) => part.trim())
    .filter(Boolean);

  if (named?.length) return [...new Set(named)];

  return [
    ...new Set(
      (job.offices ?? [])
        .map((office) => (office.name ?? office.location ?? "").trim())
        .filter(Boolean),
    ),
  ];
}

/** Boards may publish a "Location Type" custom field; most do not. */
function locationTypeFlag(job: GreenhouseJob): string | null {
  const entry = (job.metadata ?? []).find(
    (item) => item.name?.toLowerCase() === "location type",
  );
  if (!entry?.value) return null;
  return Array.isArray(entry.value) ? entry.value.join(" ") : entry.value;
}

export function normalizeGreenhouseJob(
  slug: string,
  job: GreenhouseJob,
): NormalizedJob {
  const locations = splitLocations(job);
  const location = primaryLocation(locations);
  const descriptionRaw = job.content ?? null;
  const cleaned = cleanDescription(descriptionRaw);

  const remote = detectRemote({
    location: [locations.join(" "), locationTypeFlag(job) ?? ""].join(" "),
    title: job.title,
    description: cleaned.text,
  });

  const salary = parseSalary(cleaned.text);
  const posted = job.first_published ?? job.updated_at ?? null;

  return {
    id: jobId("greenhouse", slug, String(job.id)),
    source: "greenhouse",
    externalId: String(job.id),
    companySlug: slug,
    companyName: job.company_name?.trim() || slug,
    title: job.title,
    locations,
    locationNorm: location,
    remote,
    employmentType: null,
    salary,
    descriptionRaw,
    descriptionText: cleaned.text || null,
    url: job.absolute_url,
    // Greenhouse's board page is also its application form.
    applyUrl: null,
    postedAt: posted ? new Date(posted) : null,
    raw: job,
    contentHash: contentHash({
      title: job.title,
      locations,
      descriptionRaw,
      salaryMin: salary.min,
      salaryMax: salary.max,
    }),
    dedupeKey: dedupeKey({
      companyName: job.company_name?.trim() || slug,
      title: job.title,
      primaryLocation: location?.city ?? location?.raw ?? null,
    }),
  };
}

export const greenhouse: JobSourceAdapter = {
  id: "greenhouse",

  async fetchBoard(slug: string): Promise<FetchBoardResult> {
    const url = `${BASE}/${encodeURIComponent(slug)}/jobs?content=true`;

    let body: string;
    try {
      body = await fetchText(url);
    } catch (error) {
      if (error instanceof HttpError && error.status === 404) {
        throw new BoardNotFoundError("greenhouse", slug);
      }
      throw error;
    }

    const board = JSON.parse(body) as GreenhouseBoard;
    return {
      companySlug: slug,
      jobs: (board.jobs ?? []).map((job) => normalizeGreenhouseJob(slug, job)),
    };
  },
};
