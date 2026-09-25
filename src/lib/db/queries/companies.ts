import { and, asc, eq, inArray, lt, or, sql } from "drizzle-orm";

import { db } from "@/lib/db";
import { companies, type Company, type NewCompany } from "@/lib/db/schema";

/**
 * The shared company registry.
 *
 * Unlike almost everything else in queries/, this is NOT user-scoped:
 * `companies` is part of the shared mirror, so one person adding Stripe means
 * everyone benefits from the fetch. `added_by_user_id` records who asked, not
 * who owns it.
 */

export async function listCompanies(options?: {
  status?: Company["status"];
  limit?: number;
}) {
  const where = options?.status
    ? eq(companies.status, options.status)
    : undefined;

  return db
    .select()
    .from(companies)
    .where(where)
    .orderBy(asc(companies.name))
    .limit(options?.limit ?? 500);
}

export async function getCompanyBySlug(slug: string) {
  const [row] = await db
    .select()
    .from(companies)
    .where(eq(companies.slug, slug))
    .limit(1);
  return row ?? null;
}

/** Finds a company by the board it is registered against. */
export async function getCompanyByBoard(
  sourceType: Company["sourceType"],
  atsSlug: string,
) {
  const [row] = await db
    .select()
    .from(companies)
    .where(
      and(eq(companies.sourceType, sourceType), eq(companies.atsSlug, atsSlug)),
    )
    .limit(1);
  return row ?? null;
}

/**
 * Registers a company, or updates the one already holding that board.
 *
 * Idempotent on `(source_type, ats_slug)` so discovery running twice — or two
 * users adding the same company — converges rather than duplicating.
 */
export async function upsertCompany(input: NewCompany): Promise<Company> {
  const existing =
    (input.atsSlug
      ? await getCompanyByBoard(input.sourceType, input.atsSlug)
      : null) ?? (await getCompanyBySlug(input.slug));

  if (existing) {
    const [updated] = await db
      .update(companies)
      .set({
        name: input.name,
        domain: input.domain ?? existing.domain,
        careersUrls: input.careersUrls ?? existing.careersUrls,
        // Merged, never replaced: discovery re-registering a company it
        // already knows sends no tags, and must not erase the ones it has.
        tags: input.tags?.length
          ? [...new Set([...existing.tags, ...input.tags])]
          : existing.tags,
        // A company that was marked invalid and now verifies is active again.
        status: input.status ?? existing.status,
        needsBrowser: input.needsBrowser ?? existing.needsBrowser,
        updatedAt: new Date(),
      })
      .where(eq(companies.id, existing.id))
      .returning();
    return updated;
  }

  const [created] = await db.insert(companies).values(input).returning();
  return created;
}

/** Boards that have not been fetched recently enough to trust. */
export async function staleCompanies(staleBefore: Date, limit = 200) {
  return db
    .select()
    .from(companies)
    .where(
      and(
        eq(companies.status, "active"),
        or(
          sql`${companies.lastFetchedAt} is null`,
          lt(companies.lastFetchedAt, staleBefore),
        ),
      ),
    )
    .orderBy(sql`${companies.lastFetchedAt} asc nulls first`)
    .limit(limit);
}

export async function recordFetch(input: {
  companyId: string;
  status: string;
  jobCount: number;
  markInvalid?: boolean;
}) {
  await db
    .update(companies)
    .set({
      lastFetchedAt: new Date(),
      lastFetchStatus: input.status,
      lastFetchJobCount: input.jobCount,
      ...(input.markInvalid ? { status: "invalid" as const } : {}),
      updatedAt: new Date(),
    })
    .where(eq(companies.id, input.companyId));
}

export async function setCompanyStatus(
  id: string,
  status: Company["status"],
) {
  const [row] = await db
    .update(companies)
    .set({ status, updatedAt: new Date() })
    .where(eq(companies.id, id))
    .returning();
  return row ?? null;
}

export async function countByStatus() {
  return db
    .select({ status: companies.status, count: sql<number>`count(*)::int` })
    .from(companies)
    .groupBy(companies.status);
}

/** Which of these slugs we already know about, so discovery can skip them. */
export async function knownSlugs(slugs: string[]): Promise<Set<string>> {
  if (slugs.length === 0) return new Set();
  const rows = await db
    .select({ slug: companies.slug })
    .from(companies)
    .where(inArray(companies.slug, slugs));
  return new Set(rows.map((row) => row.slug));
}

/**
 * Registered companies for names a user typed ("Anthropic", "stripe.com").
 * Matches the name ignoring case and punctuation, our slug, the board slug,
 * or the domain. Returns what each name resolved to, in the order given, and
 * the names that matched nothing.
 */
export async function findCompaniesByNames(names: string[]): Promise<{
  found: Array<{ name: string; company: Company }>;
  missing: string[];
}> {
  const found: Array<{ name: string; company: Company }> = [];
  const missing: string[] = [];

  for (const name of names) {
    const key = name.toLowerCase().replace(/[^a-z0-9]+/g, "");
    const domain = name.trim().toLowerCase().replace(/^https?:\/\//, "").replace(/^www\./, "").split("/")[0] ?? "";
    if (!key) continue;

    const [company] = await db
      .select()
      .from(companies)
      .where(
        and(
          eq(companies.status, "active"),
          or(
            sql`regexp_replace(lower(${companies.name}), '[^a-z0-9]+', '', 'g') = ${key}`,
            sql`regexp_replace(${companies.slug}, '[^a-z0-9]+', '', 'g') = ${key}`,
            sql`regexp_replace(lower(coalesce(${companies.atsSlug}, '')), '[^a-z0-9]+', '', 'g') = ${key}`,
            domain.includes(".") ? eq(companies.domain, domain) : undefined,
          ),
        ),
      )
      // Prefer a board over a careers page when a name has both.
      .orderBy(sql`case when ${companies.sourceType} in ('greenhouse', 'lever', 'ashby') then 0 else 1 end`)
      .limit(1);

    if (company) found.push({ name, company });
    else missing.push(name);
  }

  return { found, missing };
}
