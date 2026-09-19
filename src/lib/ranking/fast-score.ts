import { and, eq, inArray, isNull, sql } from "drizzle-orm";

import { db } from "@/lib/db";
import { jobMatches, jobs } from "@/lib/db/schema";
import type { LlmContext } from "@/lib/llm/context";
import { generateStructured } from "@/lib/llm/structured";
import { pooled } from "@/lib/pool";
import type { Preferences } from "@/lib/profile/interview";
import type { Resume } from "@/lib/resume/schema";

import { FastScore } from "./schemas";

/**
 * The cheap scoring pass.
 *
 * Runs over every posting the prefilter kept — hundreds per run — so the unit
 * cost is what matters. Three things keep it down: a small output schema, a
 * truncated description, and a prompt whose expensive half (the rubric and the
 * candidate) is identical across every call in a run, so providers that cache
 * prompt prefixes charge for it once rather than hundreds of times.
 *
 * Its job is triage, not judgement. Anything needing real reasoning about a
 * specific posting belongs in the deep evaluation, which runs over the top
 * forty rather than the top six hundred.
 */

/** How much of a posting the cheap pass reads. */
const MAX_DESCRIPTION_CHARS = 4_000;
/** Below this, a match is archived rather than shown. */
export const DEFAULT_SCORE_THRESHOLD = 55;
/**
 * Concurrent scoring calls.
 *
 * Each call takes about two seconds regardless, so this is what decides how
 * long a run feels. Measured: 16 postings took 6.1s at 8-way and 3.0s at
 * 16-way. The per-provider limiter is the real ceiling above this, which is
 * where an account's actual rate limit belongs.
 */
const CONCURRENCY = 16;

const RUBRIC = `You triage job postings for one candidate. Score 0–100.

The scale, which is not a feeling:
- 85–100: squarely what they asked for. Right level, right domain, right
  location, and their background is a direct match.
- 70–84: a good fit with a gap they could close, or a slightly different domain.
- 55–69: plausible. Adjacent domain, or the level is a little off, or the
  posting is vague enough that it might be right.
- 30–54: probably wrong — wrong level, wrong discipline, or a hard requirement
  they do not meet.
- 0–29: clearly not this person.

Rules:
- Judge against what the posting actually says. If it does not state something,
  that is not a miss — it is unknown.
- hard_requirement_misses is only for requirements the posting states and the
  candidate demonstrably does not meet. A "nice to have" is never a miss, and
  neither is a technology they have not listed.
- seniority is the posting's level, not the candidate's.
- fit_reasons: at most three, short, concrete. No restating the job title.
- Be decisive. A middling score for everything is useless triage.`;

/** The half of the prompt that never changes within a run. */
function candidateBlock(input: {
  preferences: Preferences;
  resume: Resume | null;
}): string {
  const resume = input.resume;

  const experience = (resume?.work ?? [])
    .slice(0, 6)
    .map(
      (role) =>
        `- ${role.position} at ${role.company} (${role.startDate ?? "?"}–${role.endDate ?? "present"})` +
        (role.highlights.length
          ? `\n  ${role.highlights.slice(0, 3).join("\n  ")}`
          : ""),
    )
    .join("\n");

  const education = (resume?.education ?? [])
    .map(
      (item) =>
        `- ${item.studyType ?? ""} ${item.area ?? ""} at ${item.institution} (${item.endDate ?? "?"})`,
    )
    .join("\n");

  const skills = (resume?.skills ?? []).map((skill) => skill.name).join(", ");

  return `<candidate>
Wants: ${JSON.stringify(input.preferences)}

Experience:
${experience || "(none on file)"}

Education:
${education || "(none on file)"}

Skills: ${skills || "(none on file)"}
</candidate>`;
}

export type ScoreOutcome = {
  jobId: string;
  score: number | null;
  archived: boolean;
  error?: string;
};

export async function scoreMatches(input: {
  userId: string;
  preferences: Preferences;
  resume: Resume | null;
  jobIds: string[];
  threshold?: number;
  context?: LlmContext;
  /** Checked between batches so a cancelled run stops scoring. */
  shouldStop?: () => Promise<boolean>;
  onProgress?: (done: number, total: number) => Promise<void>;
}): Promise<{ scored: number; archived: number; failed: number }> {
  const threshold = input.threshold ?? DEFAULT_SCORE_THRESHOLD;

  // Anything already scored is skipped: re-scoring an unchanged posting buys
  // nothing and is the easiest way to double a run's bill.
  const pending = await db
    .select({ jobId: jobMatches.jobId })
    .from(jobMatches)
    .where(
      and(
        eq(jobMatches.userId, input.userId),
        inArray(jobMatches.jobId, input.jobIds),
        isNull(jobMatches.fastScore),
      ),
    );

  const toScore = pending.map((row) => row.jobId);
  if (toScore.length === 0) return { scored: 0, archived: 0, failed: 0 };

  const candidate = candidateBlock(input);

  let scored = 0;
  let archived = 0;
  let failed = 0;
  let done = 0;

  // Batched so cancellation is noticed within seconds rather than at the end.
  const BATCH = 40;

  for (let offset = 0; offset < toScore.length; offset += BATCH) {
    if (await input.shouldStop?.()) break;

    const batch = toScore.slice(offset, offset + BATCH);

    const postings = await db
      .select({
        id: jobs.id,
        title: jobs.title,
        company: jobs.companyName,
        locations: jobs.locations,
        remote: jobs.remote,
        salaryMin: jobs.salaryMin,
        salaryMax: jobs.salaryMax,
        description: jobs.descriptionText,
      })
      .from(jobs)
      .where(inArray(jobs.id, batch));

    const outcomes = await pooled(
      postings,
      async (posting): Promise<ScoreOutcome> => {
        try {
          const { value } = await generateStructured(
            "score",
            FastScore,
            {
              // Rubric and candidate first, posting last: the stable prefix is
              // what a provider's prompt cache can reuse across the run.
              instructions: `${RUBRIC}\n\n${candidate}`,
              prompt: `<posting>
Title: ${posting.title}
Company: ${posting.company}
Location: ${posting.locations.join(" | ") || "unstated"} (${posting.remote})
Pay: ${posting.salaryMin ? `${posting.salaryMin}–${posting.salaryMax}` : "unstated"}

${posting.description?.slice(0, MAX_DESCRIPTION_CHARS) ?? "(no description)"}
</posting>`,
            },
            input.context ?? {},
          );

          const below = value.score < threshold;

          await db
            .update(jobMatches)
            .set({
              fastScore: value.score,
              fastEval: value,
              ...(below
                ? {
                    stage: "archived" as const,
                    stageChangedAt: new Date(),
                    notes: `Auto-archived: scored ${value.score}, below the ${threshold} threshold.`,
                  }
                : {}),
              updatedAt: new Date(),
            })
            .where(
              and(
                eq(jobMatches.userId, input.userId),
                eq(jobMatches.jobId, posting.id),
                // Never drag a job the user has already acted on back to
                // archived because a cheap model disliked it.
                eq(jobMatches.stage, "found"),
              ),
            );

          return { jobId: posting.id, score: value.score, archived: below };
        } catch (error) {
          // A posting we could not score stays unscored and visible, rather
          // than being silently dropped.
          return {
            jobId: posting.id,
            score: null,
            archived: false,
            error: error instanceof Error ? error.message : String(error),
          };
        }
      },
      CONCURRENCY,
    );

    for (const outcome of outcomes) {
      if (outcome.error) failed += 1;
      else {
        scored += 1;
        if (outcome.archived) archived += 1;
      }
    }

    done += batch.length;
    await input.onProgress?.(done, toScore.length);
  }

  return { scored, archived, failed };
}

/** Score distribution for a user, for the run summary and the pipeline page. */
export async function scoreSummary(userId: string) {
  const [row] = await db
    .select({
      scored: sql<number>`count(*) filter (where ${jobMatches.fastScore} is not null)::int`,
      strong: sql<number>`count(*) filter (where ${jobMatches.fastScore} >= 80)::int`,
      good: sql<number>`count(*) filter (where ${jobMatches.fastScore} >= 65 and ${jobMatches.fastScore} < 80)::int`,
      archived: sql<number>`count(*) filter (where ${jobMatches.stage} = 'archived')::int`,
    })
    .from(jobMatches)
    .where(eq(jobMatches.userId, userId));

  return row ?? { scored: 0, strong: 0, good: 0, archived: 0 };
}
