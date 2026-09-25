import { and, eq } from "drizzle-orm";
import { ArrowLeft, Clock, ExternalLink, MapPin, Wallet } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { CompanyMark, PageBody, Section } from "@/components/foray/primitives";
import { RefreshOn } from "@/components/live/refresh-on";
import {
  DeepEvaluation,
  FastEvaluation,
  type DeepEvalView,
  type FastEvalView,
} from "@/components/matches/evaluation";
import { EvaluateButton, MatchNotes, StageSelect } from "@/components/matches/match-controls";
import { Posting } from "@/components/matches/posting";
import { TailorPanel, type TailoredView } from "@/components/matches/tailor-panel";
import { buttonVariants } from "@/components/ui/button";
import { requireUser } from "@/lib/auth/session";
import { db } from "@/lib/db";
import { getMatchDetail } from "@/lib/db/queries/matches";
import { getBaseResume } from "@/lib/db/queries/profile";
import { resumes } from "@/lib/db/schema";
import {
  formatLocation,
  formatSalary,
  postedLabel,
  remoteLabel,
  STAGE_LABEL,
  timeAgo,
  type StageId,
} from "@/lib/format";

type Params = { params: Promise<{ id: string }> };

export async function generateMetadata({ params }: Params): Promise<Metadata> {
  const user = await requireUser();
  const detail = await getMatchDetail(user.id, (await params).id);
  return { title: detail ? `${detail.job.title} at ${detail.job.companyName}` : "Match" };
}

export default async function MatchPage({ params }: Params) {
  const user = await requireUser();
  const { id } = await params;

  const detail = await getMatchDetail(user.id, id);
  if (!detail) notFound();
  const { match, job } = detail;

  const [tailoredRow, base] = await Promise.all([
    match.tailoredResumeId
      ? db
          .select()
          .from(resumes)
          .where(and(eq(resumes.id, match.tailoredResumeId), eq(resumes.userId, user.id)))
          .limit(1)
          .then((rows) => rows[0] ?? null)
      : Promise.resolve(null),
    getBaseResume(user.id),
  ]);

  const tailored: TailoredView | null = tailoredRow
    ? {
        id: tailoredRow.id,
        documentId: tailoredRow.renderedDocumentId,
        createdAt: tailoredRow.createdAt.toISOString(),
        changes: (tailoredRow.changes as TailoredView["changes"]) ?? [],
        verification: (tailoredRow.verification as TailoredView["verification"]) ?? null,
      }
    : null;

  const deep = match.deepEval as DeepEvalView | null;
  const fast = match.fastEval as FastEvalView | null;
  const salary = formatSalary(job);
  const remote = remoteLabel(job.remote);

  return (
    <PageBody>
      <RefreshOn kinds={["run.completed", "tailor.done", "render.done"]} />

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
          <StageSelect matchId={match.id} stage={match.stage as StageId} />
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
              <TailorPanel hasResume={Boolean(base)} matchId={match.id} tailored={tailored} />
            </div>
          </Section>

          <Section title="Notes">
            <MatchNotes initial={match.notes} matchId={match.id} />
          </Section>

          <Section title="History">
            <dl className="text-[13px]">
              <Row label="Found" value={timeAgo(match.createdAt)} />
              <Row label={STAGE_LABEL[match.stage as StageId]} value={`since ${timeAgo(match.stageChangedAt)}`} />
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
