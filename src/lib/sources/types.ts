import type { jobSource, remoteKind } from "@/lib/db/schema";

/**
 * What every source produces, whatever shape it arrived in.
 *
 * Adapters normalize; models hold data. No source-specific branching may leak
 * past this boundary — if scoring or the UI ever needs to know which ATS a job
 * came from in order to read a field, that field belongs here instead.
 */

export type JobSourceId = (typeof jobSource.enumValues)[number];
export type RemoteKind = (typeof remoteKind.enumValues)[number];

/** A location after normalization; `raw` is always what the source said. */
export type NormalizedLocation = {
  raw: string;
  city: string | null;
  region: string | null;
  country: string | null;
  /** Our own grouping, e.g. `bay-area`. Null when we do not recognise it. */
  metro: string | null;
};

export type NormalizedSalary = {
  min: number | null;
  max: number | null;
  currency: string | null;
  /** As the source states it. Never converted between periods. */
  period: "year" | "month" | "hour" | null;
};

export type NormalizedJob = {
  /** `source:company_slug:external_id` — DESIGN.md invariant 2. */
  id: string;
  source: JobSourceId;
  externalId: string;
  companySlug: string;
  companyName: string;

  title: string;
  locations: string[];
  locationNorm: NormalizedLocation | null;
  remote: RemoteKind;
  employmentType: string | null;

  salary: NormalizedSalary;

  /** Exactly as fetched, never rewritten — DESIGN.md invariant 4. */
  descriptionRaw: string | null;
  /** HTML stripped and known boilerplate removed. Derived, disposable. */
  descriptionText: string | null;

  url: string;
  applyUrl: string | null;
  postedAt: Date | null;

  /** The source's own payload, so a parser fix can be replayed. */
  raw: unknown;
  contentHash: string;
  dedupeKey: string;
};

export type FetchBoardResult = {
  companySlug: string;
  jobs: NormalizedJob[];
};

/**
 * A source of postings.
 *
 * Board sources fetch one company at a time; aggregators (JSearch, Step 13)
 * search across many, which is why `search` is separate rather than
 * `fetchBoard` with a magic slug.
 */
export interface JobSourceAdapter {
  readonly id: JobSourceId;
  /** Fetches every current posting for one company's board. */
  fetchBoard?(slug: string): Promise<FetchBoardResult>;
}

/** Thrown when a board does not exist, as opposed to being empty. */
export class BoardNotFoundError extends Error {
  constructor(
    readonly source: JobSourceId,
    readonly slug: string,
  ) {
    super(`No ${source} board for slug "${slug}"`);
    this.name = "BoardNotFoundError";
  }
}
