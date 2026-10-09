import type { Metadata } from "next";

import { CompanyTable } from "@/components/companies/company-table";
import { RefreshStaleButton } from "@/components/companies/refresh-stale-button";
import { PageBody, PageHeader } from "@/components/foray/primitives";
import { RefreshOn } from "@/components/live/refresh-on";
import type { components } from "@/lib/api/schema";
import { django } from "@/lib/api/server";
import { plural } from "@/lib/format";

export const metadata: Metadata = { title: "Companies" };

type CompaniesPageData = components["schemas"]["CompaniesPage"];

/**
 * The registry: which companies Foray watches, and whether their boards
 * actually answered last time.
 *
 * Shared across users, so Django does not scope it by user — but it still
 * requires a signed-in user, since it is part of the app rather than a public
 * page.
 */
export default async function CompaniesPage() {
  const { companies, active, openPostings } = await django<CompaniesPageData>(
    "/api/pages/companies",
  );

  return (
    <PageBody>
      <RefreshOn kinds={["run.completed", "company.discovered", "company.refresh.done"]} />
      <PageHeader
        actions={<RefreshStaleButton />}
        description={
          companies.length === 0
            ? "The companies whose job boards Foray reads. Every search starts from these boards, then looks wider."
            : `${plural(active, "active board")} with ${plural(openPostings, "open posting")} between them. Foray refreshes them every six hours and keeps postings that disappear, marked closed.`
        }
        title="Companies"
      />
      <div className="mt-8">
        <CompanyTable companies={companies} />
      </div>
    </PageBody>
  );
}
