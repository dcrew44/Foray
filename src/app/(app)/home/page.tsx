import { Check } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";

import {
  EmptyState,
  PageBody,
  Section,
} from "@/components/foray/primitives";
import { AskBox } from "@/components/home/ask-box";
import { AttentionPanel, RecentActivity, StartSearchButton } from "@/components/home/live-panels";
import { OnboardingChat } from "@/components/home/onboarding-chat";
import { RefreshOn } from "@/components/live/refresh-on";
import { fromMatchRow, MatchLine } from "@/components/matches/match-line";
import { ResumeDropzone } from "@/components/profile/resume-dropzone";
import { buttonVariants } from "@/components/ui/button";
import { requireUser } from "@/lib/auth/session";
import { countByStage, listMatches, OPEN_STAGES } from "@/lib/db/queries/matches";
import { monthStart, spendSince } from "@/lib/db/queries/spend";
import { db } from "@/lib/db";
import { approvals, runs } from "@/lib/db/schema";
import { formatUsd, plural, STAGE_LABEL } from "@/lib/format";
import { getOnboardingState, type OnboardingState } from "@/lib/profile/onboarding";
import { cn } from "@/lib/utils";
import { and, eq, inArray, sql } from "drizzle-orm";

export const metadata: Metadata = { title: "Home" };

export default async function HomePage() {
  const user = await requireUser();

  const [onboarding, counts, best, spend, waiting] = await Promise.all([
    getOnboardingState(user.id),
    countByStage(user.id),
    listMatches(user.id, { stages: ["found", "shortlisted"], limit: 6 }),
    spendSince(user.id, monthStart()),
    liveCounts(user.id),
  ]);

  const firstName = (user.name || "").split(" ")[0];
  const { headline, detail } = describeState({ onboarding, counts, best, waiting, firstName });

  const suggestions = onboarding.ready
    ? [
        onboarding.preferences.target_roles?.[0]
          ? `Find new ${onboarding.preferences.target_roles[0].toLowerCase()} roles`
          : "Find new roles for me",
        "Which of my matches fit me best, and why?",
        best[0] ? `Tailor my resume for the ${best[0].company} role` : "What should I improve on my resume?",
      ]
    : onboarding.hasResume
      ? ["Help me set up what I'm looking for", "What does my resume say about me?"]
      : [];

  return (
    <PageBody>
      <RefreshOn kinds={["run.completed", "approval.decided", "approval.requested", "tailor.done"]} />

      <div className="max-w-3xl">
        <h1 className="text-display text-[34px] leading-[1.08] sm:text-[42px]">{headline}</h1>
        {detail && (
          <p className="text-muted-foreground mt-3 max-w-[60ch] text-[15px] leading-relaxed">
            {detail}
          </p>
        )}
        {onboarding.hasResume && <AskBox className="mt-7" suggestions={suggestions} />}
      </div>

      {!onboarding.ready ? (
        <Onboarding onboarding={onboarding} />
      ) : (
        <div className="mt-12 grid gap-12 xl:grid-cols-[minmax(0,1fr)_300px]">
          <div className="min-w-0 space-y-10">
            <AttentionPanel />

            <Section
              action={
                best.length > 0 && (
                  <Link className="text-muted-foreground hover:text-foreground" href="/matches">
                    Open the board
                  </Link>
                )
              }
              description={best.length > 0 ? "Found and shortlisted, highest fit first." : undefined}
              title="Best matches"
            >
              {best.length === 0 ? (
                <EmptyState
                  action={<StartSearchButton label="Run a search" />}
                  title="No open matches yet"
                >
                  Run a search with your saved preferences, or ask Foray for something
                  specific above.
                </EmptyState>
              ) : (
                <div className="-mx-3">
                  {best.map((row) => (
                    <MatchLine key={row.matchId} match={fromMatchRow(row)} showHeadline />
                  ))}
                </div>
              )}
            </Section>
          </div>

          <aside className="grid content-start gap-10 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-1">
            <Section title="Your board">
              <PipelineSummary counts={counts} />
            </Section>

            <RecentActivity />

            <Section title="This month">
              <dl className="grid grid-cols-2 gap-4">
                <div>
                  <dt className="text-muted-foreground text-[12.5px]">Model spend</dt>
                  <dd className="text-display tabular mt-1 text-[22px]">{formatUsd(spend.usd)}</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground text-[12.5px]">Model calls</dt>
                  <dd className="text-display tabular mt-1 text-[22px]">{spend.calls.toLocaleString()}</dd>
                </div>
              </dl>
              <Link className="text-muted-foreground hover:text-foreground text-[12.5px]" href="/settings#usage">
                See the breakdown
              </Link>
            </Section>
          </aside>
        </div>
      )}
    </PageBody>
  );
}

async function liveCounts(userId: string) {
  const [pending, active] = await Promise.all([
    db
      .select({ n: sql<number>`count(*)::int` })
      .from(approvals)
      .where(
        and(
          eq(approvals.userId, userId),
          eq(approvals.status, "pending"),
          sql`(${approvals.expiresAt} is null or ${approvals.expiresAt} > now())`,
        ),
      ),
    db
      .select({ n: sql<number>`count(*)::int` })
      .from(runs)
      .where(and(eq(runs.userId, userId), inArray(runs.status, ["queued", "running"]))),
  ]);
  return { approvals: pending[0]?.n ?? 0, running: active[0]?.n ?? 0 };
}

/** The headline is a status report, not a greeting. */
function describeState(input: {
  onboarding: OnboardingState;
  counts: Record<string, number>;
  best: Awaited<ReturnType<typeof listMatches>>;
  waiting: { approvals: number; running: number };
  firstName: string;
}): { headline: string; detail: string | null } {
  const { onboarding, counts, best, waiting, firstName } = input;
  const hi = firstName ? `${firstName}, ` : "";

  if (!onboarding.hasResume) {
    return {
      headline: "Let's set up your search.",
      detail:
        "Start with your resume. Foray builds your profile from it, asks a few questions about what you want, then goes looking.",
    };
  }
  if (!onboarding.ready) {
    return {
      headline: "A few answers and Foray can start searching.",
      detail: `Still needed: ${onboarding.progress.blocking.map((slot) => slot.label.toLowerCase()).join(", ")}. Answer in chat, or fill them in on your profile.`,
    };
  }
  if (waiting.approvals > 0) {
    return {
      headline:
        waiting.approvals === 1
          ? `${hi}one decision is waiting on you.`
          : `${hi}${waiting.approvals} decisions are waiting on you.`,
      detail: null,
    };
  }
  if (waiting.running > 0) {
    return { headline: "Foray is searching right now.", detail: "Results land below as they come in." };
  }

  const strong = best.filter((row) => (row.score ?? 0) >= 80).length;
  const queue = counts.found + counts.shortlisted;
  if (queue > 0) {
    return {
      headline:
        strong > 0
          ? `${plural(strong, "strong match", "strong matches")} waiting for a look.`
          : `${plural(queue, "role")} in your queue.`,
      detail:
        counts.shortlisted > 0
          ? `${counts.shortlisted} shortlisted, ${counts.applied} applied so far.`
          : null,
    };
  }
  return {
    headline: "Ready when you are.",
    detail: "Ask for what you want below, or run a search with your saved preferences.",
  };
}

function PipelineSummary({ counts }: { counts: Record<string, number> }) {
  const max = Math.max(1, ...OPEN_STAGES.map((stage) => counts[stage] ?? 0));

  return (
    <ul className="space-y-2">
      {OPEN_STAGES.map((stage) => {
        const count = counts[stage] ?? 0;
        return (
          <li key={stage}>
            <Link
              className="hover:bg-muted/60 -mx-2 grid grid-cols-[92px_minmax(0,1fr)_32px] items-center gap-3 rounded-md px-2 py-1 text-[13px]"
              href={`/matches?stage=${stage}`}
            >
              <span className="text-foreground/80">{STAGE_LABEL[stage]}</span>
              <span className="bg-muted h-1.5 overflow-hidden rounded-full">
                <span
                  className={cn("block h-full rounded-full", stage === "found" ? "bg-fit-3" : "bg-fit-4")}
                  style={{ width: `${(count / max) * 100}%` }}
                />
              </span>
              <span className="tabular text-right font-medium">{count}</span>
            </Link>
          </li>
        );
      })}
      {(counts.archived ?? 0) + (counts.rejected ?? 0) > 0 && (
        <li className="text-muted-foreground pt-1 text-[12px]">
          {counts.archived + counts.rejected} archived or rejected
        </li>
      )}
    </ul>
  );
}

/**
 * The three steps before a first search. These genuinely are a sequence — the
 * interview needs the resume, the search needs both — so they are numbered.
 */
function Onboarding({ onboarding }: { onboarding: OnboardingState }) {
  const resume = onboarding.resume;
  const interviewDone = onboarding.progress.blocking.length === 0;

  return (
    <ol className="mt-10 max-w-3xl space-y-4">
      <OnboardingStep
        done={onboarding.hasResume}
        number={1}
        title="Add your resume"
      >
        {onboarding.hasResume && resume ? (
          <p className="text-muted-foreground text-[13.5px]">
            {plural(resume.work.length, "role")}, {plural(resume.education.length, "school")} and{" "}
            {plural(resume.skills.length, "skill")} on file.{" "}
            <Link className="text-primary font-medium hover:underline" href="/profile">
              Review your profile
            </Link>
          </p>
        ) : (
          <ResumeDropzone hasResume={false} />
        )}
      </OnboardingStep>

      <OnboardingStep
        done={interviewDone}
        locked={!onboarding.hasResume}
        number={2}
        title="Tell Foray what you're looking for"
      >
        {onboarding.hasResume && !interviewDone && (
          <div className="space-y-3">
            <p className="text-muted-foreground text-[13.5px]">
              {onboarding.progress.answered} of {onboarding.progress.total} answered. Foray
              needs your {onboarding.progress.blocking.map((slot) => slot.label.toLowerCase()).join(", ")}.
            </p>
            <div className="flex flex-wrap gap-2">
              <OnboardingChat />
              <Link className={buttonVariants({ variant: "outline" })} href="/profile#preferences">
                Fill in a form instead
              </Link>
            </div>
          </div>
        )}
        {interviewDone && (
          <p className="text-muted-foreground text-[13.5px]">Preferences saved.</p>
        )}
      </OnboardingStep>

      <OnboardingStep locked={!onboarding.ready} number={3} title="Run your first search">
        {onboarding.ready && <StartSearchButton label="Start searching" />}
      </OnboardingStep>
    </ol>
  );
}

function OnboardingStep({
  number,
  title,
  done = false,
  locked = false,
  children,
}: {
  number: number;
  title: string;
  done?: boolean;
  locked?: boolean;
  children?: React.ReactNode;
}) {
  return (
    <li
      className={cn(
        "flex gap-4 rounded-2xl border px-5 py-4",
        locked && "opacity-55",
        !done && !locked && "border-primary/30 bg-accent/40",
      )}
    >
      <span
        className={cn(
          "text-display inline-flex size-8 shrink-0 items-center justify-center rounded-full text-[14px]",
          done ? "bg-fit-4 text-fit-4-foreground" : "bg-background border",
        )}
      >
        {done ? <Check className="size-4" strokeWidth={3} /> : number}
      </span>
      <div className="min-w-0 flex-1 space-y-2 pt-1">
        <p className="text-[15px] font-semibold">{title}</p>
        {children}
      </div>
    </li>
  );
}
