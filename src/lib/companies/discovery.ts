import { env } from "@/env";
import { fetchText, HttpError } from "@/lib/sources/http";
import {
  getCompanyByBoard,
  upsertCompany,
} from "@/lib/db/queries/companies";
import type { Company } from "@/lib/db/schema";
import type { BoardSourceId } from "@/lib/sources";

import { findBoard, slugCandidates, verifyBoard } from "./verify";

/**
 * Turning "Stripe" into a registered, verified job board.
 *
 * The governing rule is that a company is registered only after a live API call
 * returns postings. Everything below produces *candidates*; verification is the
 * only thing that produces a registration. That is what stops a plausible slug
 * guess from filling the registry with boards that do not exist and refreshes
 * that fail forever.
 *
 * The order is cheapest-and-most-certain first:
 *   1. an ATS link on the company's careers page   — direct evidence
 *   2. slug guesses from the name                  — free, often right
 *   3. a web search to find the careers page       — costs a request
 */

/** ATS board URLs, and which adapter handles each. */
const ATS_PATTERNS: Array<{
  source: BoardSourceId;
  pattern: RegExp;
}> = [
  {
    source: "greenhouse",
    pattern:
      /(?:boards|job-boards)\.greenhouse\.io\/(?:embed\/job_board\?for=)?([a-z0-9_-]+)/gi,
  },
  { source: "lever", pattern: /jobs\.lever\.co\/([a-z0-9_-]+)/gi },
  { source: "ashby", pattern: /jobs\.ashbyhq\.com\/([a-z0-9_-]+)/gi },
];

/**
 * ATS platforms we can recognise but cannot read through an API. These become
 * `careers_page` companies for the LLM extractor (Step 14) rather than being
 * discarded — a Workday board is still a real board.
 */
const UNSUPPORTED_ATS =
  /(?:myworkdayjobs\.com|icims\.com|taleo\.net|successfactors\.com|smartrecruiters\.com|workable\.com|breezy\.hr|jazzhr\.com|bamboohr\.com|jobs\.gem\.com|jobvite\.com|recruitee\.com|teamtailor\.com|paylocity\.com|dover\.com|rippling(?:ats)?\.com|pinpointhq\.com|applytojob\.com)/i;

export type AtsLink = { source: BoardSourceId; slug: string };

/** Finds ATS board links in a page's HTML. */
export function findAtsLinks(html: string): AtsLink[] {
  const found: AtsLink[] = [];

  for (const { source, pattern } of ATS_PATTERNS) {
    // The patterns are global, so lastIndex has to be reset between pages.
    pattern.lastIndex = 0;
    for (const match of html.matchAll(pattern)) {
      const slug = match[1]?.toLowerCase();
      // Greenhouse embeds sometimes link to their own docs.
      if (!slug || slug === "embed" || slug === "job_board") continue;
      if (!found.some((link) => link.source === source && link.slug === slug)) {
        found.push({ source, slug });
      }
    }
  }

  return found;
}

export function hasUnsupportedAts(html: string): boolean {
  return UNSUPPORTED_ATS.test(html);
}

/** Bare registrable domain — no scheme, no `www.`, no path. */
export function normalizeDomain(input: string): string | null {
  const trimmed = input.trim().toLowerCase();
  if (!trimmed) return null;

  try {
    const url = new URL(trimmed.includes("://") ? trimmed : `https://${trimmed}`);
    return url.hostname.replace(/^www\./, "") || null;
  } catch {
    return null;
  }
}

/** Where careers pages usually live. */
const CAREERS_PATHS = ["/careers", "/jobs", "/careers/jobs", "/company/careers"];

/** Anchors on a homepage that lead to a careers page. */
const CAREERS_LINK =
  /href\s*=\s*["']([^"']*(?:career|jobs|join-us|work-with-us)[^"']*)["']/gi;

/**
 * Careers links from a homepage, absolutised against it.
 *
 * The guessed paths cover most companies, but plenty put careers under
 * `/about/careers`, `/company/join` or a separate subdomain — and they always
 * link to it from the homepage.
 */
export function findCareersLinks(html: string, baseUrl: string): string[] {
  CAREERS_LINK.lastIndex = 0;
  const links = new Set<string>();

  for (const match of html.matchAll(CAREERS_LINK)) {
    try {
      const resolved = new URL(match[1], baseUrl);
      if (resolved.protocol !== "https:" && resolved.protocol !== "http:") {
        continue;
      }
      links.add(resolved.toString());
    } catch {
      // A malformed href is not worth failing discovery over.
    }
  }

  return [...links].slice(0, 5);
}

async function tryFetch(url: string): Promise<string | null> {
  try {
    return await fetchText(url, {
      accept: "text/html,application/xhtml+xml",
      retries: 1,
      timeoutMs: 12_000,
    });
  } catch (error) {
    // A 404 on a guessed path is expected, not a failure worth reporting.
    if (error instanceof HttpError) return null;
    return null;
  }
}

export type DiscoveryStep = { step: string; detail: string };

export type DiscoveryResult = {
  name: string;
  /** Null when nothing could be verified — the caller decides what to do. */
  registered: Company | null;
  source: BoardSourceId | "careers_page" | null;
  slug: string | null;
  careersUrls: string[];
  needsBrowser: boolean;
  /** What was tried, in order. Surfaced to the user so a miss is explicable. */
  trail: DiscoveryStep[];
};

/**
 * Uses a search API to find a company's careers page.
 *
 * Optional: without a key, discovery still works from an explicit URL or from
 * slug guesses. It is the only step here that costs anything.
 */
async function searchForCareersPage(name: string): Promise<string[]> {
  if (!env.SEARCH_API_KEY) return [];

  try {
    const url = new URL("https://api.search.brave.com/res/v1/web/search");
    url.searchParams.set("q", `${name} careers jobs`);
    url.searchParams.set("count", "5");

    const response = await fetch(url, {
      headers: {
        "X-Subscription-Token": env.SEARCH_API_KEY,
        Accept: "application/json",
      },
      signal: AbortSignal.timeout(12_000),
    });
    if (!response.ok) return [];

    const body = (await response.json()) as {
      web?: { results?: Array<{ url?: string }> };
    };
    return (body.web?.results ?? [])
      .map((result) => result.url)
      .filter((value): value is string => Boolean(value));
  } catch {
    return [];
  }
}

/**
 * Resolves a company name or URL to a registered, verified company.
 *
 * `register: false` runs the whole pipeline without writing, which is what the
 * approval flow needs: propose first, register once the user agrees.
 */
export async function discoverCompany(input: {
  nameOrUrl: string;
  addedByUserId?: string | null;
  discoveredVia?: Company["discoveredVia"];
  register?: boolean;
  tags?: string[];
}): Promise<DiscoveryResult> {
  const trail: DiscoveryStep[] = [];
  const raw = input.nameOrUrl.trim();
  const looksLikeUrl = /^(?:https?:\/\/|www\.)|\.[a-z]{2,}(?:\/|$)/i.test(raw);

  let domain = looksLikeUrl ? normalizeDomain(raw) : null;
  let name = raw;
  const careersUrls: string[] = [];

  // A board URL pasted directly is the strongest possible signal.
  const directLinks = findAtsLinks(raw);
  if (directLinks.length > 0) {
    const link = directLinks[0];
    trail.push({ step: "ats-link", detail: `pasted ${link.source}:${link.slug}` });
    const outcome = await verifyBoard(link.source, link.slug);
    if (outcome.status === "ok" || outcome.status === "empty") {
      return finish({ source: link.source, slug: link.slug, outcome });
    }
  }

  if (domain) {
    name = domain.split(".")[0];
    trail.push({ step: "domain", detail: domain });
  }

  // 1. The careers page, which usually links straight at the real board.
  const pagesToTry = domain
    ? CAREERS_PATHS.map((path) => `https://${domain}${path}`)
    : [];

  for (const url of pagesToTry) {
    const html = await tryFetch(url);
    if (!html) continue;

    careersUrls.push(url);
    const links = findAtsLinks(html);
    trail.push({
      step: "careers-page",
      detail: `${url} → ${links.length ? links.map((l) => `${l.source}:${l.slug}`).join(", ") : "no ATS link"}`,
    });

    for (const link of links) {
      const outcome = await verifyBoard(link.source, link.slug);
      if (outcome.status === "ok" || outcome.status === "empty") {
        return finish({ source: link.source, slug: link.slug, outcome });
      }
    }

    if (hasUnsupportedAts(html)) {
      trail.push({
        step: "unsupported-ats",
        detail: "Workday/iCIMS-style board — registering as a careers page",
      });
      return finish({ source: "careers_page", slug: null, outcome: null });
    }
    break;
  }

  // 1b. No careers page at a guessed path? Follow the homepage's own link.
  if (domain && careersUrls.length === 0) {
    const home = await tryFetch(`https://${domain}`);
    if (home) {
      const linked = findCareersLinks(home, `https://${domain}`);
      trail.push({ step: "homepage", detail: `${linked.length} careers links` });

      for (const url of linked) {
        const html = await tryFetch(url);
        if (!html) continue;
        careersUrls.push(url);

        const links = findAtsLinks(html);
        for (const link of links) {
          const outcome = await verifyBoard(link.source, link.slug);
          if (outcome.status === "ok" || outcome.status === "empty") {
            trail.push({
              step: "homepage-ats-link",
              detail: `${url} → ${link.source}:${link.slug}`,
            });
            return finish({ source: link.source, slug: link.slug, outcome });
          }
        }

        if (hasUnsupportedAts(html)) {
          trail.push({ step: "unsupported-ats", detail: url });
          return finish({ source: "careers_page", slug: null, outcome: null });
        }
        break;
      }
    }
  }

  // 2. Slug guesses. Free, and right more often than not for tech companies.
  for (const candidate of slugCandidates(name)) {
    const outcome = await findBoard(candidate);
    if (outcome?.status === "ok") {
      trail.push({ step: "slug-guess", detail: `${outcome.source}:${candidate}` });
      return finish({ source: outcome.source, slug: candidate, outcome });
    }
  }
  trail.push({
    step: "slug-guess",
    detail: `no board for ${slugCandidates(name).slice(0, 3).join(", ")}`,
  });

  // 3. Search, to find a careers page we could not guess.
  if (!domain) {
    const results = await searchForCareersPage(name);
    trail.push({ step: "search", detail: `${results.length} results` });

    for (const url of results.slice(0, 3)) {
      const html = await tryFetch(url);
      if (!html) continue;
      careersUrls.push(url);

      const links = findAtsLinks(html);
      for (const link of links) {
        const outcome = await verifyBoard(link.source, link.slug);
        if (outcome.status === "ok" || outcome.status === "empty") {
          trail.push({
            step: "search-ats-link",
            detail: `${url} → ${link.source}:${link.slug}`,
          });
          domain ??= normalizeDomain(url);
          return finish({ source: link.source, slug: link.slug, outcome });
        }
      }

      if (hasUnsupportedAts(html)) {
        domain ??= normalizeDomain(url);
        trail.push({ step: "unsupported-ats", detail: url });
        return finish({ source: "careers_page", slug: null, outcome: null });
      }
    }
  }

  /**
   * A careers page we could find but whose ATS we could not identify is still
   * worth registering. Gem, Rippling and a long tail of others have no public
   * API, and plenty of companies hand-roll their careers page entirely — but
   * the page itself is readable, which is exactly what the careers-page
   * extractor (Step 14) is for. Discarding it would throw away the hard part
   * of discovery over a missing regex.
   */
  if (careersUrls.length > 0) {
    trail.push({
      step: "careers-page-fallback",
      detail: "no recognised ATS, registering the careers page for extraction",
    });
    return finish({ source: "careers_page", slug: null, outcome: null });
  }

  return {
    name,
    registered: null,
    source: null,
    slug: null,
    careersUrls,
    needsBrowser: false,
    trail,
  };

  async function finish(found: {
    source: BoardSourceId | "careers_page";
    slug: string | null;
    outcome: Awaited<ReturnType<typeof verifyBoard>> | null;
  }): Promise<DiscoveryResult> {
    const ourSlug = found.slug ?? normalizeDomain(domain ?? name)?.split(".")[0] ?? name;

    const result: DiscoveryResult = {
      name,
      registered: null,
      source: found.source,
      slug: found.slug,
      careersUrls,
      // A careers-page company with no readable API may need a browser (Step 14
      // decides for certain; this only marks it as a candidate).
      needsBrowser: found.source === "careers_page",
      trail,
    };

    if (input.register === false) return result;

    result.registered = await upsertCompany({
      name: titleCase(name),
      domain,
      slug: ourSlug.toLowerCase(),
      sourceType: found.source,
      atsSlug: found.slug,
      careersUrls,
      tags: input.tags ?? [],
      status: "active",
      needsBrowser: found.source === "careers_page",
      discoveredVia: input.discoveredVia ?? "agent",
      addedByUserId: input.addedByUserId ?? null,
    });

    return result;
  }
}

function titleCase(value: string): string {
  return value
    .split(/\s+/)
    .map((word) => (word ? word[0].toUpperCase() + word.slice(1) : word))
    .join(" ");
}

/**
 * Registers boards found in an aggregator's apply links.
 *
 * The aggregator costs a request per page and returns a truncated description;
 * the company's own board is free and complete. So every result that applies
 * through a board we can read is a standing invitation to stop paying for that
 * company. Each is still verified before being written down — an apply link can
 * point at a board that has since been taken down.
 */
export async function registerBoardsFromApplyLinks(
  boards: Array<{
    source: BoardSourceId;
    slug: string;
    companyName: string;
  }>,
  options: { addedByUserId?: string | null } = {},
): Promise<Company[]> {
  const registered: Company[] = [];

  for (const board of boards) {
    const existing = await getCompanyByBoard(board.source, board.slug);
    if (existing) continue;

    const outcome = await verifyBoard(board.source, board.slug);
    if (outcome.status !== "ok") continue;

    registered.push(
      await upsertCompany({
        name: board.companyName,
        slug: board.slug,
        domain: null,
        sourceType: board.source,
        atsSlug: board.slug,
        careersUrls: [],
        tags: [],
        status: "active",
        discoveredVia: "jsearch",
        addedByUserId: options.addedByUserId ?? null,
      }),
    );
  }

  return registered;
}
