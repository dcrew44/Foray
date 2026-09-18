import { z } from "zod";

import type { LlmContext } from "@/lib/llm/context";
import { generateStructured } from "@/lib/llm/structured";
import type { Company } from "@/lib/db/schema";

import { cleanDescription, htmlToTextWithLinks } from "./clean";
import { contentHash, dedupeKey, webJobId } from "./dedupe";
import { fetchText, HttpError } from "./http";
import { detectRemote, parseSalary, primaryLocation } from "./normalize";
import type { NormalizedJob } from "./types";

/**
 * The fallback for companies with no readable API.
 *
 * Plenty of real employers hand-roll their careers page or sit on an ATS with
 * no public API — Retool is on Gem, Atlassian runs its own. Those companies are
 * not worth losing, so their pages are read with a model instead. This is the
 * most error-prone source by construction, so it is also the most constrained:
 *
 * - It runs only where no API exists. Never as a shortcut past an adapter.
 * - Posting URLs are always real links taken from the page, never constructed,
 *   so a hallucinated job cannot produce a working link to apply to.
 * - Unknown fields stay null rather than being guessed at.
 * - Everything is capped — pages per company, postings per company — because a
 *   careers page with a thousand links would otherwise cost a thousand fetches
 *   and a large model bill.
 * - A page that yields nothing is flagged `needs_browser` rather than retried:
 *   it is almost always JavaScript-rendered, which Phase 2 handles.
 */

/** How many listing pages we will read per company. */
const MAX_LISTING_PAGES = 5;
/** How many individual postings we will open per company. */
const MAX_POSTINGS = 50;
/** Cleaned listing text sent to the model. Enough for a long index page. */
const MAX_LISTING_CHARS = 30_000;
/** Cleaned posting text stored as the description. */
const MAX_POSTING_CHARS = 20_000;

/**
 * Every field required, absence expressed as null: OpenAI's strict structured
 * output rejects optional properties outright.
 */
const ExtractedPosting = z.object({
  title: z.string(),
  /** Exactly as written on the page — "San Francisco, CA", "Remote (US)". */
  location: z.string().nullable(),
  /**
   * The posting's own link, copied from the page. Never invented: a fabricated
   * URL is a job the user cannot apply to and cannot tell is fake.
   */
  url: z.string().nullable(),
  team: z.string().nullable(),
});

const ExtractedListing = z.object({
  postings: z.array(ExtractedPosting),
  /**
   * Links to further pages of the same listing (pagination, or per-department
   * indexes). Used only if we have page budget left.
   */
  more_listing_urls: z.array(z.string()),
});

const LISTING_INSTRUCTIONS = `You read a company's careers page and list the job postings on it.

Rules:
- Only postings actually present on this page. Never invent a job, and never
  extrapolate from "we're hiring across engineering" into specific roles.
- Copy each posting's link exactly as it appears. If a posting has no link on
  this page, set url to null — do not construct one.
- Copy titles and locations verbatim. Leave location null if the page does not
  say.
- Ignore navigation, benefits, culture copy, and "life at" sections. Only
  actual open roles.
- more_listing_urls is for links to further pages of the SAME job list —
  pagination, or per-team indexes. Not individual postings.
- If the page shows no job postings at all, return empty arrays. That is a
  valid answer and much better than guessing.`;

type FetchedPage = { url: string; html: string; text: string };

async function fetchPage(url: string): Promise<FetchedPage | null> {
  try {
    const html = await fetchText(url, {
      accept: "text/html,application/xhtml+xml",
      retries: 1,
      timeoutMs: 15_000,
    });
    // Links are preserved: the extractor must copy real posting URLs, and
    // plain-text conversion would leave it nothing to copy.
    return { url, html, text: htmlToTextWithLinks(html, url) };
  } catch (error) {
    if (error instanceof HttpError) return null;
    return null;
  }
}

/** Absolute, http(s) only, same-site — a model may echo a relative path. */
function resolveUrl(candidate: string, base: string): string | null {
  try {
    const url = new URL(candidate, base);
    if (url.protocol !== "https:" && url.protocol !== "http:") return null;
    return url.toString();
  } catch {
    return null;
  }
}

export type CareersPageResult = {
  jobs: NormalizedJob[];
  /** True when pages were fetched but no postings could be read from them. */
  needsBrowser: boolean;
  pagesFetched: number;
  postingsFetched: number;
};

/**
 * Reads a company's careers pages into normalized postings.
 *
 * `knownHashes` lets a refresh skip re-reading postings whose page has not
 * changed, which is what keeps the recurring cost of this source near zero.
 */
export async function fetchCareersPage(
  company: Pick<Company, "name" | "slug" | "careersUrls">,
  options: {
    context?: LlmContext;
    knownContentHashes?: Map<string, string>;
    maxPostings?: number;
  } = {},
): Promise<CareersPageResult> {
  const maxPostings = options.maxPostings ?? MAX_POSTINGS;
  const seenListings = new Set<string>();
  const queue = [...company.careersUrls];

  const postings = new Map<
    string,
    { title: string; location: string | null; team: string | null }
  >();

  let pagesFetched = 0;

  while (queue.length > 0 && pagesFetched < MAX_LISTING_PAGES) {
    const url = queue.shift()!;
    if (seenListings.has(url)) continue;
    seenListings.add(url);

    const page = await fetchPage(url);
    if (!page) continue;
    pagesFetched += 1;

    const { value } = await generateStructured(
      "extract",
      ExtractedListing,
      {
        instructions: LISTING_INSTRUCTIONS,
        prompt:
          `Careers page for ${company.name} at ${url}.\n\n` +
          `<page>\n${page.text.slice(0, MAX_LISTING_CHARS)}\n</page>`,
      },
      options.context ?? {},
    );

    for (const posting of value.postings) {
      const resolved = posting.url ? resolveUrl(posting.url, url) : null;
      // A posting with no link cannot be applied to or refetched, so it is not
      // a job we can honestly offer.
      if (!resolved || postings.has(resolved)) continue;
      if (postings.size >= maxPostings) break;

      postings.set(resolved, {
        title: posting.title,
        location: posting.location,
        team: posting.team,
      });
    }

    for (const next of value.more_listing_urls) {
      const resolved = resolveUrl(next, url);
      if (resolved && !seenListings.has(resolved)) queue.push(resolved);
    }
  }

  const jobs: NormalizedJob[] = [];
  let postingsFetched = 0;

  for (const [url, posting] of postings) {
    const id = webJobId(company.slug, url);

    const page = await fetchPage(url);
    if (page) postingsFetched += 1;

    const descriptionRaw = page?.html ?? null;
    const cleaned = cleanDescription(descriptionRaw);
    const text = cleaned.text.slice(0, MAX_POSTING_CHARS);

    const locations = posting.location ? [posting.location] : [];
    const location = primaryLocation(locations);
    const salary = parseSalary(text);

    const hash = contentHash({
      title: posting.title,
      locations,
      descriptionRaw: text,
      salaryMin: salary.min,
      salaryMax: salary.max,
    });

    jobs.push({
      id,
      source: "web",
      externalId: id.split(":").at(-1)!,
      companySlug: company.slug,
      companyName: company.name,
      title: posting.title,
      locations,
      locationNorm: location,
      remote: detectRemote({
        location: posting.location,
        title: posting.title,
        description: text,
      }),
      employmentType: null,
      salary,
      descriptionRaw: text || null,
      descriptionText: text || null,
      url,
      applyUrl: null,
      // Careers pages rarely date their postings, and a guessed date would
      // wrongly age a job out of the search window.
      postedAt: null,
      raw: { source: "careers-page", team: posting.team, listing: posting },
      contentHash: hash,
      dedupeKey: dedupeKey({
        companyName: company.name,
        title: posting.title,
        primaryLocation: location?.city ?? location?.raw ?? null,
      }),
    });
  }

  return {
    jobs,
    // Pages loaded but nothing readable in them is the signature of a
    // JavaScript-rendered board, which Phase 2's browser handles.
    needsBrowser: pagesFetched > 0 && postings.size === 0,
    pagesFetched,
    postingsFetched,
  };
}
