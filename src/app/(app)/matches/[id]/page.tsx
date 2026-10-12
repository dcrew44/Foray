import { ArrowLeft, Clock, ExternalLink, MapPin, Wallet } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";

import { CompanyMark, PageBody, Section } from "@/components/foray/primitives";
import { RefreshOn } from "@/components/live/refresh-on";
import { DeepEvaluation, FastEvaluation } from "@/components/matches/evaluation";
import { EvaluateButton, MatchNotes, StageSelect } from "@/components/matches/match-controls";
import { Posting } from "@/components/matches/posting";
import { TailorPanel } from "@/components/matches/tailor-panel";
import { buttonVariants } from "@/components/ui/button";
import type { components } from "@/lib/api/schema";
import { django } from "@/lib/api/server";
import {
  formatLocation,
  formatSalary,
  postedLabel,
  remoteLabel,
  STAGE_LABEL,
  timeAgo,
} from "@/lib/format";

type MatchDetailPage = components["schemas"]["MatchDetailPage"];
type Params = { params: Promise<{ id: string }> };

/**
 * One match in one read (`GET /api/pages/matches/{id}`): the match with its stored
 * evaluations, the posting, its tailored resume and whether a base resume exists. A malformed
 * id, an unknown one and another user's all answer 404, which `django()` turns into
 * `notFound()`. The title and the page make the same GET, which Next memoises within a render,
 * so they cost one call between them.
 */
function load(id: string) {
  return django<MatchDetailPage>(`/api/pages/matches/${encodeURIComponent(id)}`);
}

export async function generateMetadata({ params }: Params): Promise<Metadata> {
  const { job } = await load((await params).id);
  return { title: `${job.title} at ${job.companyName}` };
}

export default async function MatchPage({ params }: Params) {
  const { id } = await params;
  const { match, job, tailored, hasBaseResume } = await load(id);

  const deep = match.deepEval;
  const fast = match.fastEval;
  const salary = formatSalary(job);
  const remote = remoteLabel(job.remote);

  return (
    <PageBody>
      {/* Tailoring and rendering end with run.completed too, so one kind covers them. */}
      <RefreshOn kinds={["run.completed"]} />

      <Link
        className="text-muted-foreground hover:text-foreground mb-6 inline-flex items-center gap-1.5 text-[13px]"
        href="/matches"
      >
        <ArrowLeft className="size-4" />
        Matches
      </Link>

      <header className="flex flex-col gap-6 lg:flex-row lg:items-start lg:justify-between">
        <div className="flex min-w-0 gap-4">
          <CompanyMark className="mt-1" name={job.companyName} size="lg" />
          <div className="min-w-0">
            <h1 className="text-display text-[26px] leading-[1.15] sm:text-[30px]">{job.title}</h1>
            <p className="text-foreground/85 mt-1 text-[15px] font-medium">{job.companyName}</p>
            <div className="text-muted-foreground mt-3 flex flex-wrap gap-x-5 gap-y-1.5 text-[13px]">
              <span className="inline-flex items-center gap-1.5">
                <MapPin className="size-3.5" />
                {formatLocation(job.locations, null)}
                {remote && <span className="text-foreground/70">({remote.toLowerCase()})</span>}
              </span>
              {salary && (
                <span className="inline-flex items-center gap-1.5">
                  <Wallet className="size-3.5" />
                  {salary}
                </span>
              )}
              {job.postedAt && (
                <span className="inline-flex items-center gap-1.5">
                  <Clock className="size-3.5" />
                  {postedLabel(job.postedAt)}
                </span>
              )}
              {!job.isActive && <span className="text-destructive">This posting has closed</span>}
            </div>
          </div>
        </div>

        <div className="flex shrink-0 flex-wrap items-center gap-2">
          <StageSelect matchId={match.id} stage={match.stage} />
          <a className={buttonVariants({ variant: "outline" })} href={job.url} rel="noreferrer" target="_blank">
            <ExternalLink />
            View posting
          </a>
          {job.applyUrl && job.applyUrl !== job.url && (
            <a className={buttonVariants()} href={job.applyUrl} rel="noreferrer" target="_blank">
              Apply
            </a>
          )}
        </div>
      </header>

      <div className="mt-10 grid gap-12 xl:grid-cols-[minmax(0,1fr)_380px]">
        <div className="min-w-0 space-y-12">
          <Section
            action={deep ? <EvaluateButton label="Review again" matchId={match.id} /> : undefined}
            title="Why it fits"
          >
            {deep ? (
              <DeepEvaluation evaluation={deep} />
            ) : fast ? (
              <div className="space-y-6">
                <FastEvaluation evaluation={fast} />
                <EvaluateButton matchId={match.id} />
              </div>
            ) : (
              <div className="space-y-3">
                <p className="text-muted-foreground text-[13.5px]">
                  This role has not been scored yet.
                </p>
                <EvaluateButton matchId={match.id} />
              </div>
            )}
          </Section>

          <Section title="The posting">
            <Posting text={job.descriptionText} />
          </Section>
        </div>

        <aside className="space-y-10">
          <Section title="Tailored resume">
            <div id="resume" className="scroll-mt-6">
              <TailorPanel hasResume={hasBaseResume} matchId={match.id} tailored={tailored} />
            </div>
          </Section>

          <Section title="Notes">
            <MatchNotes initial={match.notes} matchId={match.id} />
          </Section>

          <Section title="History">
            <dl className="text-[13px]">
              <Row label="Found" value={timeAgo(match.createdAt)} />
              <Row label={STAGE_LABEL[match.stage]} value={`since ${timeAgo(match.stageChangedAt)}`} />
              {match.fastScore != null && <Row label="Triage score" value={String(match.fastScore)} />}
              {match.deepScore != null && <Row label="Deep review" value={String(match.deepScore)} />}
              <Row label="Source" value={job.source === "jsearch" ? "Job aggregator" : job.source === "web" ? "Careers page" : `${job.source[0]!.toUpperCase()}${job.source.slice(1)} board`} />
            </dl>
          </Section>
        </aside>
      </div>
    </PageBody>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-4 border-b py-2 last:border-b-0">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="tabular text-right">{value}</dd>
    </div>
  );
}
