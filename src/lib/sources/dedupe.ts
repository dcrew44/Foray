import { createHash } from "node:crypto";

/**
 * Recognising the same job arriving from two places.
 *
 * The aggregator (Step 13) and a company's own ATS board both carry the same
 * posting, and a user should see it once. The key is deliberately coarse —
 * company, title, primary location — because the two copies rarely agree on
 * anything finer: descriptions get truncated, ids differ, and posted dates
 * drift by days.
 */

/** Suffixes ATS boards append to a company name but which are not part of it. */
const COMPANY_SUFFIX =
  /\b(?:inc|inc\.|llc|l\.l\.c\.|ltd|ltd\.|limited|corp|corp\.|corporation|co|co\.|gmbh|sa|s\.a\.|plc|pty|ag|nv|bv)\b/gi;

/**
 * Bracketed and trailing qualifiers on a title.
 *
 * "(Remote)", "- San Francisco", "[Contract]" and requisition numbers vary
 * between the same job's two listings, so they are stripped before hashing.
 */
const TITLE_NOISE = [
  /\([^)]*\)/g,
  /\[[^\]]*\]/g,
  /\bREQ[-\s#]?\d+\b/gi,
  /\b(?:#\s*)?\d{4,}\b/g,
  /\s+[-–—]\s+(?:remote|hybrid|on-?site|contract|full[-\s]?time|part[-\s]?time)\b.*$/i,
];

export function normalizeCompany(name: string): string {
  return name
    .toLowerCase()
    .replace(/&/g, " and ")
    .replace(COMPANY_SUFFIX, " ")
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

export function normalizeTitle(title: string): string {
  let cleaned = title.toLowerCase();
  for (const pattern of TITLE_NOISE) cleaned = cleaned.replace(pattern, " ");

  // `+` and `#` survive so "C++" and "C#" stay distinguishable; seniority
  // numerals (II, III) are kept, since Engineer II and Engineer III are
  // genuinely different jobs.
  return cleaned.replace(/[^a-z0-9+#]+/g, " ").trim();
}

export function normalizeLocationForKey(location: string | null): string {
  if (!location) return "";
  return location
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

/**
 * `sha1(company | title | primary location)`.
 *
 * sha1 rather than something stronger because this is a bucketing key, not a
 * security boundary — a collision means two similar jobs merge, not a breach.
 */
export function dedupeKey(input: {
  companyName: string;
  title: string;
  primaryLocation?: string | null;
}): string {
  const parts = [
    normalizeCompany(input.companyName),
    normalizeTitle(input.title),
    normalizeLocationForKey(input.primaryLocation ?? null),
  ];
  return createHash("sha1").update(parts.join("|")).digest("hex");
}

/**
 * Whether a posting's substance changed since we last saw it.
 *
 * Keyed on what a user would notice — not on `fetched_at`, which changes on
 * every poll and is cache metadata rather than a job fact.
 */
export function contentHash(input: {
  title: string;
  locations: string[];
  descriptionRaw?: string | null;
  salaryMin?: number | null;
  salaryMax?: number | null;
}): string {
  return createHash("sha1")
    .update(
      JSON.stringify([
        input.title,
        [...input.locations].sort(),
        input.descriptionRaw ?? "",
        input.salaryMin ?? null,
        input.salaryMax ?? null,
      ]),
    )
    .digest("hex");
}

/** `source:company_slug:external_id` — DESIGN.md invariant 2. */
export function jobId(
  source: string,
  companySlug: string,
  externalId: string,
): string {
  return `${source}:${companySlug}:${externalId}`;
}

/** Career-page postings have no id of their own, so the URL is the identity. */
export function webJobId(companySlug: string, url: string): string {
  return jobId(
    "web",
    companySlug,
    createHash("sha1").update(url).digest("hex"),
  );
}
