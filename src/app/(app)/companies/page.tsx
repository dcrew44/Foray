import type { Metadata } from "next";

import { CompanyTable } from "@/components/companies/company-table";
import { RadarPanel } from "@/components/companies/radar-panel";
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
  const { companies, active, openPostings, radar } = await django<CompaniesPageData>(
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
            : `${plural(active, "active board")} with ${plural(openPostings, "open posting")} between them. Foray asks each board whether it changed, reads the ones that did, and keeps postings that disappear, marked closed.`
        }
        title="Companies"
      />
      {companies.length > 0 && (
        <div className="mt-6">
          <RadarPanel radar={radar} />
        </div>
      )}
      <div className="mt-8">
        <CompanyTable companies={companies} />
      </div>
    </PageBody>
  );
}
