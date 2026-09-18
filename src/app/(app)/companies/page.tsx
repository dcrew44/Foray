import { asc, count, eq } from "drizzle-orm";

import {
  CompanyTable,
  type CompanyRow,
} from "@/components/companies/company-table";
import { requireUser } from "@/lib/auth/session";
import { db } from "@/lib/db";
import { companies, jobs } from "@/lib/db/schema";

/**
 * The registry: which companies Foray watches, and whether their boards
 * actually answered last time.
 *
 * Shared across users, so this is not user-scoped — but it still requires a
 * signed-in user, since it is part of the app rather than a public page.
 */
export default async function CompaniesPage() {
  await requireUser();

  /**
   * Companies and their open-posting counts, as two queries rather than a
   * correlated subquery. Drizzle renders column references inside a raw `sql`
   * fragment unqualified, so the subquery form compared jobs.company_id to
   * jobs.id and failed at the database. Two small queries merged here are
   * clearer and stay type-safe.
   */
  const [rows, counts, [{ total = 0 } = {}]] = await Promise.all([
    db
      .select()
      .from(companies)
      .orderBy(asc(companies.status), asc(companies.name))
      .limit(500),
    db
      .select({
        companyId: jobs.companyId,
        activeJobs: count(jobs.id),
      })
      .from(jobs)
      .where(eq(jobs.isActive, true))
      .groupBy(jobs.companyId),
    db
      .select({ total: count(jobs.id) })
      .from(jobs)
      .where(eq(jobs.isActive, true)),
  ]);

  const jobsByCompany = new Map(
    counts.map((row) => [row.companyId, row.activeJobs]),
  );

  const data: CompanyRow[] = rows.map((row) => ({
    slug: row.slug,
    name: row.name,
    sourceType: row.sourceType,
    atsSlug: row.atsSlug,
    status: row.status,
    tags: row.tags,
    lastFetchStatus: row.lastFetchStatus,
    lastFetchJobCount: row.lastFetchJobCount,
    discoveredVia: row.discoveredVia,
    lastFetchedAt: row.lastFetchedAt?.toISOString() ?? null,
    activeJobs: jobsByCompany.get(row.id) ?? 0,
  }));

  const active = rows.filter((row) => row.status === "active").length;

  return (
    <main className="mx-auto w-full max-w-5xl space-y-8 p-8">
      <div className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">Companies</h1>
        <p className="text-muted-foreground text-sm">
          {active} active {active === 1 ? "board" : "boards"} ·{" "}
          {total.toLocaleString()} open{" "}
          {total === 1 ? "posting" : "postings"} in the mirror. Boards refresh
          every six hours; postings that disappear are kept and marked closed.
        </p>
      </div>

      <CompanyTable companies={data} />
    </main>
  );
}
