import { asc, count, eq } from "drizzle-orm";
import type { Metadata } from "next";

import { toCompanyRow } from "@/components/companies/company-row";
import { CompanyTable } from "@/components/companies/company-table";
import { RefreshStaleButton } from "@/components/companies/refresh-stale-button";
import { PageBody, PageHeader } from "@/components/foray/primitives";
import { RefreshOn } from "@/components/live/refresh-on";
import { requireUser } from "@/lib/auth/session";
import { db } from "@/lib/db";
import { companies, jobs } from "@/lib/db/schema";
import { plural } from "@/lib/format";

export const metadata: Metadata = { title: "Companies" };

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
  const [rows, counts] = await Promise.all([
    db.select().from(companies).orderBy(asc(companies.name)).limit(2000),
    db
      .select({ companyId: jobs.companyId, activeJobs: count(jobs.id) })
      .from(jobs)
      .where(eq(jobs.isActive, true))
      .groupBy(jobs.companyId),
  ]);

  const jobsByCompany = new Map(counts.map((row) => [row.companyId, row.activeJobs]));
  const data = rows.map((row) => toCompanyRow(row, jobsByCompany.get(row.id) ?? 0));

  const active = rows.filter((row) => row.status === "active").length;
  const onBoards = data.reduce((sum, row) => sum + row.activeJobs, 0);

  return (
    <PageBody>
      <RefreshOn kinds={["company.refresh.done", "company.discovered"]} />
      <PageHeader
        actions={<RefreshStaleButton />}
        description={
          rows.length === 0
            ? "The companies whose job boards Foray reads. Every search starts from these boards, then looks wider."
            : `${plural(active, "active board")} with ${plural(onBoards, "open posting")} between them. Foray refreshes them every six hours and keeps postings that disappear, marked closed.`
        }
        title="Companies"
      />
      <div className="mt-8">
        <CompanyTable companies={data} />
      </div>
    </PageBody>
  );
}
