import { ChevronDown, ExternalLink } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";

import { ContactSearch } from "@/components/contacts/contact-search";
import { ImportBox } from "@/components/contacts/import-box";
import { LookupPanel } from "@/components/contacts/lookup-panel";
import { RemoveAllContacts, RemoveContactButton } from "@/components/contacts/remove-contacts";
import { CompanyMark, EmptyState, PageBody, PageHeader, Section, Stat } from "@/components/foray/primitives";
import { RefreshOn } from "@/components/live/refresh-on";
import type { components } from "@/lib/api/schema";
import { django } from "@/lib/api/server";
import { connectedLabel } from "@/lib/contacts";
import { plural } from "@/lib/format";

export const metadata: Metadata = { title: "Contacts" };

type ContactsPageData = components["schemas"]["ContactsPage"];
type ContactPerson = components["schemas"]["ContactPerson"];
type SearchParams = Record<string, string | string[] | undefined>;

/**
 * The people the user knows, from files they exported themself, in one read
 * (`GET /api/pages/contacts`): how many work at each company Foray reads, the
 * companies it does not read yet, and a search over all of them.
 *
 * Foray uses contacts for one thing: saying when a posting is at a company where
 * the user knows someone. It contacts nobody.
 */
export default async function ContactsPage({
  searchParams,
}: {
  searchParams: Promise<SearchParams>;
}) {
  const params = await searchParams;
  const raw = Array.isArray(params.q) ? params.q[0] : params.q;
  const asked = raw?.trim() ?? "";
  const page = await django<ContactsPageData>(
    asked ? `/api/pages/contacts?q=${encodeURIComponent(asked)}` : "/api/pages/contacts",
  );
  const { total, matched, companies, companiesTotal, unmatched, unmatchedTotal, lookup } = page;

  return (
    <PageBody>
      {/* A lookup that registers a board changes which list a company is in. */}
      <RefreshOn kinds={["run.completed", "company.discovered"]} />
      <PageHeader
        description="People you know, from files you export yourself. Foray uses them to tell you when a new posting is at a company where you know someone. It contacts nobody, and it never signs in to LinkedIn."
        title="Contacts"
      />

      <div className="mt-8 max-w-3xl">
        <ImportBox hasContacts={total > 0} />
      </div>

      {total === 0 ? (
        <EmptyState className="mt-8 max-w-3xl" title="No contacts yet">
          Import your LinkedIn connections, or any list of people with their companies. A
          referral asked for on the day a role is posted counts for more than one asked for
          after five hundred people have applied, so Foray will say which new postings are at
          companies where you know someone.
        </EmptyState>
      ) : (
        <div className="mt-10 space-y-12">
          <section aria-label="Totals" className="bg-card rounded-xl border px-5 py-4">
            <div className="grid gap-x-10 gap-y-5 sm:grid-cols-3">
              <Stat label={total === 1 ? "Person you know" : "People you know"} value={total.toLocaleString()} />
              <Stat
                label={matched === 1 ? "Works at a company Foray reads" : "Work at companies Foray reads"}
                value={matched.toLocaleString()}
              />
              <Stat
                label={companiesTotal === 1 ? "Company where you know someone" : "Companies where you know someone"}
                value={companiesTotal.toLocaleString()}
              />
            </div>
          </section>

          <div className="space-y-4">
            <ContactSearch query={page.query} />
            {page.results && (
              <Section
                title={
                  page.resultsTotal === 0
                    ? `Nobody matches “${page.query}”`
                    : `${plural(page.resultsTotal, "person", "people")} matching “${page.query}”`
                }
              >
                {page.results.length > 0 && (
                  <ul className="divide-y rounded-xl border text-[13.5px]">
                    {page.results.map((person) => {
                      const connected = connectedLabel(person.connectedOn);
                      return (
                        <li className="flex items-start gap-3 px-4 py-2.5" key={person.id}>
                          <div className="min-w-0 flex-1">
                            <p className="flex flex-wrap items-baseline gap-x-2.5">
                              <span className="font-medium">{person.name}</span>
                              {person.position && (
                                <span className="text-muted-foreground">{person.position}</span>
                              )}
                            </p>
                            <p className="text-muted-foreground mt-0.5 flex flex-wrap gap-x-4 text-[12.5px]">
                              <span>
                                {person.companyName || "No company given"}
                                {person.companyName && !person.companySlug && " (not read by Foray yet)"}
                              </span>
                              {connected && <span>{connected}</span>}
                              {person.email && <span>{person.email}</span>}
                              {person.profileUrl && (
                                <a
                                  className="hover:text-foreground inline-flex items-center gap-1"
                                  href={person.profileUrl}
                                  rel="noreferrer"
                                  target="_blank"
                                >
                                  Profile
                                  <ExternalLink className="size-3" />
                                </a>
                              )}
                            </p>
                          </div>
                          <RemoveContactButton id={person.id} name={person.name} />
                        </li>
                      );
                    })}
                  </ul>
                )}
                {page.resultsTotal > page.results.length && (
                  <p className="text-muted-foreground text-[12.5px]">
                    Showing the first {page.results.length}. Add a word to narrow it down.
                  </p>
                )}
              </Section>
            )}
          </div>

          <Section
            description="Companies whose job boards Foray reads, most people first. A new posting at one of these is announced with who you know there."
            title={`At companies Foray reads (${companiesTotal.toLocaleString()})`}
          >
            {companies.length === 0 ? (
              <p className="text-muted-foreground rounded-xl border border-dashed px-5 py-4 text-[13.5px]">
                None of the companies your contacts name is on Foray&apos;s list yet. Look for
                their boards below, or add one on the{" "}
                <Link className="underline" href="/companies">
                  Companies
                </Link>{" "}
                page.
              </p>
            ) : (
              <ul className="divide-y rounded-xl border text-[13.5px]">
                {companies.map((company) => (
                  <li key={company.slug}>
                    <details className="group">
                      <summary className="hover:bg-muted/40 flex cursor-pointer list-none items-center gap-3 px-4 py-2.5 [&::-webkit-details-marker]:hidden">
                        <CompanyMark name={company.name} size="sm" />
                        <span className="min-w-0 flex-1 truncate font-medium">{company.name}</span>
                        <span className="tabular shrink-0">
                          {plural(company.peopleCount, "person", "people")}
                        </span>
                        <span className="text-muted-foreground tabular hidden w-40 shrink-0 text-right sm:block">
                          {company.openPostings > 0
                            ? plural(company.openPostings, "open posting")
                            : "No open postings"}
                        </span>
                        <ChevronDown className="text-muted-foreground size-4 shrink-0 transition-transform group-open:rotate-180" />
                      </summary>
                      <People
                        more={company.peopleCount - company.people.length}
                        name={company.name}
                        people={company.people}
                      />
                    </details>
                  </li>
                ))}
              </ul>
            )}
            {companiesTotal > companies.length && (
              <p className="text-muted-foreground text-[12.5px]">
                Showing the {companies.length} companies where you know the most people. Search
                for a company to see the rest.
              </p>
            )}
          </Section>

          {unmatchedTotal > 0 && (
            <Section
              description="Companies your contacts name that are not on Foray's list, most people first."
              title={`Not read by Foray yet (${unmatchedTotal.toLocaleString()})`}
            >
              <LookupPanel lookup={lookup} unmatched={unmatchedTotal} />
              <ul className="divide-y rounded-xl border text-[13.5px]">
                {unmatched.map((company) => (
                  <li key={company.name}>
                    <details className="group">
                      <summary className="hover:bg-muted/40 flex cursor-pointer list-none items-center gap-3 px-4 py-2.5 [&::-webkit-details-marker]:hidden">
                        <span className="min-w-0 flex-1 truncate font-medium">{company.name}</span>
                        {company.lookedUp && (
                          <span className="text-muted-foreground hidden shrink-0 text-[12.5px] sm:block">
                            Looked up, no board found
                          </span>
                        )}
                        <span className="tabular w-24 shrink-0 text-right">
                          {plural(company.peopleCount, "person", "people")}
                        </span>
                        <ChevronDown className="text-muted-foreground size-4 shrink-0 transition-transform group-open:rotate-180" />
                      </summary>
                      <People
                        more={company.peopleCount - company.people.length}
                        name={company.name}
                        people={company.people}
                      />
                    </details>
                  </li>
                ))}
              </ul>
              {unmatchedTotal > unmatched.length && (
                <p className="text-muted-foreground text-[12.5px]">
                  Showing the {unmatched.length} companies most of your contacts work at.
                </p>
              )}
            </Section>
          )}

          <div className="border-t pt-6">
            <RemoveAllContacts total={total} />
          </div>
        </div>
      )}
    </PageBody>
  );
}

/** The people at one company: who they are and what they do there. */
function People({ people, more, name }: { people: ContactPerson[]; more: number; name: string }) {
  return (
    <ul className="bg-muted/20 space-y-1 border-t px-4 py-2.5">
      {people.map((person) => (
        <li className="flex items-center gap-3" key={person.id}>
          <span className="shrink-0 font-medium">{person.name}</span>
          <span className="text-muted-foreground min-w-0 flex-1 truncate">{person.position}</span>
          <RemoveContactButton id={person.id} name={person.name} />
        </li>
      ))}
      {more > 0 && (
        <li className="text-muted-foreground pt-1 text-[12.5px]">
          And {more.toLocaleString()} more.{" "}
          <Link className="underline" href={`/contacts?q=${encodeURIComponent(name)}`}>
            See everyone at {name}
          </Link>
        </li>
      )}
    </ul>
  );
}
