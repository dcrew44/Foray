import type { Metadata } from "next";
import Link from "next/link";

import { SOURCE_LABEL } from "@/components/companies/company-row";
import { EmptyState, PageBody, PageHeader, Section } from "@/components/foray/primitives";
import { FunnelPanel } from "@/components/outcomes/funnel-panel";
import { RateTable } from "@/components/outcomes/rate-table";
import { SpeedPanel } from "@/components/outcomes/speed-panel";
import { TimingPanel } from "@/components/outcomes/timing-panel";
import { buttonVariants } from "@/components/ui/button";
import { django } from "@/lib/api/server";
import {
  laneName,
  recommendationName,
  resumeName,
  type OutcomesPage as OutcomesPageData,
} from "@/lib/outcomes";

export const metadata: Metadata = { title: "Outcomes" };

/**
 * What came of the applications (#65, #6), in one read (`GET /api/pages/outcomes`): how long
 * each took to go out after its posting went up, whether the ones sent sooner got more
 * interviews, how far the matches have got, and the interview rate by source, recommendation,
 * lane and resume.
 *
 * Every number comes with the count it rests on, and Django sends a rate only when that count
 * is enough to mean something. With nothing sent yet the page says what will appear and where
 * to start, and shows no number it does not have.
 */
export default async function OutcomesPage() {
  const page = await django<OutcomesPageData>("/api/pages/outcomes");
  const { matches, applications, timing, speed, funnel } = page;

  return (
    <PageBody>
      <PageHeader
        description="How long your applications took to go out after a posting went up, and what came of them. Every number says how many applications it rests on."
        title="Outcomes"
      />

      {matches === 0 ? (
        <EmptyState
          action={
            <Link className={buttonVariants({ variant: "outline" })} href="/matches">
              Open Matches
            </Link>
          }
          className="mt-8"
          title="Nothing to measure yet"
        >
          Outcomes are counted from your matches and the applications you send. Once a search
          has found matches and you have sent an application, this page reports how long it took
          to go out and how far it got.
        </EmptyState>
      ) : (
        <div className="mt-8 max-w-4xl space-y-12">
          {applications === 0 ? (
            <EmptyState
              action={
                <Link className={buttonVariants({ variant: "outline" })} href="/apply">
                  Open Apply
                </Link>
              }
              title="No applications sent yet"
            >
              Once you send one, this page reports the time from the posting going up to your
              application going out, in three parts: until Foray saw the posting, until the
              application was ready, and until you sent it. As replies come in it also reports
              how often an application led to an interview. To start, open a prepared
              application on the Apply page, send it, and choose I sent it.
            </EmptyState>
          ) : (
            <>
              <Section
                description="The time from a posting going up to your application going out, and the three spans it is made of."
                title="Posted to applied"
              >
                <TimingPanel timing={timing} />
              </Section>

              <Section
                description="Applications by how long after the posting went up they were sent, and how many of each reached an interview."
                title="Does applying sooner help?"
              >
                <SpeedPanel minimum={funnel.minDenominator} speed={speed} />
              </Section>
            </>
          )}

          <Section
            description="How far your matches have got, from found to offer."
            title="Funnel"
          >
            <FunnelPanel funnel={funnel} />
          </Section>

          {applications > 0 && (
            <>
              <Section
                description={`Of the applications you sent, how many reached an interview or an offer. A rate is shown from ${funnel.minDenominator} applications; under that, the counts.`}
                title="Interview rate"
              >
                <div className="grid gap-x-12 gap-y-8 lg:grid-cols-2">
                  <RateTable
                    groups={funnel.bySource}
                    name={(key) => SOURCE_LABEL[key] ?? key}
                    title="By source"
                  />
                  <RateTable
                    groups={funnel.byRecommendation}
                    name={recommendationName}
                    title="By the review's recommendation"
                  />
                  <RateTable groups={funnel.byLane} name={laneName} title="By lane" />
                  <RateTable groups={funnel.byResume} name={resumeName} title="By resume" />
                </div>
              </Section>

              <Section title="How these are counted">
                <div className="text-muted-foreground max-w-[75ch] space-y-2 text-[13.5px] leading-relaxed">
                  <p>
                    A match counts as applied to once it has been in Applied, Interviewing or
                    Offer, or its application was marked sent on the Apply page, and as an
                    interview once it has been in Interviewing or Offer. It keeps both when
                    it is later rejected or archived. Moving a match back to Found or
                    Shortlisted says it was not applied to after all.
                  </p>
                  <p>
                    Foray began keeping this in November 2026. A match that was rejected or
                    archived before then counts as applied to only if its application was
                    marked sent on the Apply page, and never as an interview, so for older
                    matches these counts are the least it could be.
                  </p>
                </div>
              </Section>
            </>
          )}
        </div>
      )}
    </PageBody>
  );
}
