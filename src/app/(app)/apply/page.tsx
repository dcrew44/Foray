import type { Metadata } from "next";
import Link from "next/link";

import { ApplicationCard } from "@/components/apply/application-card";
import { EmptyState, PageBody, PageHeader, Section } from "@/components/foray/primitives";
import { RefreshOn } from "@/components/live/refresh-on";
import type { components } from "@/lib/api/schema";
import { django } from "@/lib/api/server";
import { afterPosted } from "@/lib/apply";
import { plural, timeAgo } from "@/lib/format";

export const metadata: Metadata = { title: "Apply" };

type ApplyPageData = components["schemas"]["ApplyPage"];

/**
 * What is ready to send, the posting that has been up longest first, then
 * what waits on an answer. Foray plans each application from the user's
 * resume and saved answers; the user opens the form and presses submit.
 */
export default async function ApplyPage() {
  const page = await django<ApplyPageData>("/api/pages/apply");
  const { ready, needsAnswers, failed, preparing, sent, variants } = page;
  const nothing = ready.length + needsAnswers.length + failed.length + preparing + sent.length === 0;

  return (
    <PageBody>
      <RefreshOn kinds={["application.prepared", "run.completed"]} />
      <PageHeader
        description="Applications Foray has prepared from your resume and saved answers. The posting that has been up longest comes first. Foray sends nothing: you open the form, check it and submit."
        title="Apply"
      />

      {nothing ? (
        <EmptyState className="mt-8" title="Nothing prepared yet">
          Foray prepares an application for each new posting it keeps while it watches the
          boards. To prepare one now, open a <Link className="underline" href="/matches">match</Link>{" "}
          and choose Prepare application.
        </EmptyState>
      ) : (
        <div className="mt-8 max-w-3xl space-y-12">
          {preparing > 0 && (
            <p className="text-muted-foreground text-[13.5px]" role="status">
              Reading the {preparing === 1 ? "form" : "forms"} of {plural(preparing, "application")}
              . They appear here as each one is planned.
            </p>
          )}

          {ready.length > 0 && (
            <Section
              description="Every required field has its answer."
              title={`Ready to send (${ready.length})`}
            >
              <div className="space-y-3">
                {ready.map((item) => (
                  <ApplicationCard item={item} key={item.id} variants={variants} />
                ))}
              </div>
            </Section>
          )}

          {needsAnswers.length > 0 && (
            <Section
              description="Answer once and Foray keeps it for every application that asks the same thing."
              title={`Waiting on your answer (${needsAnswers.length})`}
            >
              <div className="space-y-3">
                {needsAnswers.map((item) => (
                  <ApplicationCard item={item} key={item.id} variants={variants} />
                ))}
              </div>
            </Section>
          )}

          {failed.length > 0 && (
            <Section title={`Could not be read (${failed.length})`}>
              <div className="space-y-3">
                {failed.map((item) => (
                  <ApplicationCard item={item} key={item.id} variants={variants} />
                ))}
              </div>
            </Section>
          )}

          {sent.length > 0 && (
            <Section title="Sent">
              <ul className="divide-y rounded-xl border text-[13.5px]">
                {sent.map((item) => (
                  <li className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 px-4 py-2.5" key={item.id}>
                    <Link className="font-medium hover:underline" href={`/matches/${item.matchId}`}>
                      {item.title}
                    </Link>
                    <span className="text-muted-foreground">{item.company}</span>
                    <span className="text-muted-foreground ml-auto text-[12.5px]">
                      Sent {timeAgo(item.submittedAt)}
                      {afterPosted(item.postedToAppliedS)
                        ? `, ${afterPosted(item.postedToAppliedS)}`
                        : ""}
                    </span>
                  </li>
                ))}
              </ul>
            </Section>
          )}
        </div>
      )}
    </PageBody>
  );
}
