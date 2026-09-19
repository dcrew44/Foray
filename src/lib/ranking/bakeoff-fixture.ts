import { sql } from "drizzle-orm";

import { db } from "@/lib/db";
import { jobs } from "@/lib/db/schema";
import { registry } from "@/lib/llm/registry";
import { generateStructured } from "@/lib/llm/structured";
import type { ReasoningEffort } from "@/lib/llm/models";
import type { TokenCounts } from "@/lib/llm/pricing";
import { tokensFromProviderUsage } from "@/lib/llm/ledger";

import { FastScore } from "./schemas";

/**
 * The scoring fixture for `scripts/model-bakeoff.ts`.
 *
 * Choosing the scoring model is the highest-stakes cost decision in the
 * product: it runs hundreds of times per search, so a tenth of a cent either
 * way is the difference between a run costing 30 cents and costing three
 * dollars. That question deserves measurement rather than taste, which is what
 * this exists for.
 *
 * It scores the same fixed postings with each candidate model, at the same
 * reasoning effort, and reports cost, latency, schema-failure rate and how far
 * each model's scores drift from a reference.
 */

const RUBRIC = `You triage job postings for a candidate. Score 0-100, where 85+ is
squarely what they asked for, 55-69 is plausible, and below 30 is clearly wrong.
Be decisive: a middling score for everything is useless triage.
fit_reasons: at most three short concrete phrases.`;

const CANDIDATE = `<candidate>
Senior backend engineer, six years of experience in Node.js, TypeScript,
Python and PostgreSQL. Has owned a billing service end to end and led a
migration off a monolith. Wants senior backend roles in the San Francisco Bay
Area or remote, minimum $190k base. US citizen. Dealbreakers: on-call more than
one week a month, full return-to-office.
</candidate>`;

export type ScoredFixture = { jobId: string; score: number };

/**
 * A stable sample of real postings.
 *
 * Ordered by id so every model sees exactly the same set — a bake-off over
 * different postings would measure the postings, not the models.
 */
export async function scoringSample(limit = 20) {
  return db
    .select({
      id: jobs.id,
      title: jobs.title,
      company: jobs.companyName,
      locations: jobs.locations,
      remote: jobs.remote,
      description: jobs.descriptionText,
    })
    .from(jobs)
    .where(
      sql`${jobs.descriptionText} is not null and length(${jobs.descriptionText}) between 1500 and 12000`,
    )
    .orderBy(jobs.id)
    .limit(limit);
}

export type ScoringRunResult = {
  outputs: ScoredFixture[];
  tokens: TokenCounts;
  latencyMs: number;
  schemaFailures: number;
};

/**
 * Scores the sample with one model.
 *
 * The model is passed explicitly rather than read from env, so a bake-off can
 * compare several in one process — env is parsed once at import, which makes
 * "just change the variable between runs" quietly not work.
 */
export async function runScoringFixture(
  modelRef: string,
  options: { reasoning?: ReasoningEffort; limit?: number } = {},
): Promise<ScoringRunResult> {
  const sample = await scoringSample(options.limit ?? 20);
  const model = registry.languageModel(
    modelRef as Parameters<typeof registry.languageModel>[0],
  );

  const outputs: ScoredFixture[] = [];
  const tokens: TokenCounts = {
    inputTokens: 0,
    outputTokens: 0,
    cachedInputTokens: 0,
    cacheWriteTokens: 0,
    reasoningTokens: 0,
  };

  let schemaFailures = 0;
  const startedAt = Date.now();

  for (const posting of sample) {
    try {
      const { value, usage } = await generateStructured(
        "score",
        FastScore,
        {
          instructions: `${RUBRIC}\n\n${CANDIDATE}`,
          prompt: `<posting>
Title: ${posting.title}
Company: ${posting.company}
Location: ${posting.locations.join(" | ") || "unstated"} (${posting.remote})

${posting.description?.slice(0, 4000)}
</posting>`,
        },
        {},
        { model, reasoning: options.reasoning, providerId: modelRef.split(":")[0] },
      );

      outputs.push({ jobId: posting.id, score: value.score });

      const counted = tokensFromProviderUsage({
        inputTokens: {
          total: usage.inputTokens,
          noCache: usage.inputTokenDetails?.noCacheTokens,
          cacheRead: usage.inputTokenDetails?.cacheReadTokens,
          cacheWrite: usage.inputTokenDetails?.cacheWriteTokens,
        },
        outputTokens: {
          total: usage.outputTokens,
          reasoning: usage.outputTokenDetails?.reasoningTokens,
        },
      });

      tokens.inputTokens += counted.inputTokens;
      tokens.outputTokens += counted.outputTokens;
      tokens.cachedInputTokens! += counted.cachedInputTokens ?? 0;
      tokens.cacheWriteTokens! += counted.cacheWriteTokens ?? 0;
      tokens.reasoningTokens! += counted.reasoningTokens ?? 0;
    } catch {
      // Counted rather than thrown: a model that fails a fifth of the time is
      // exactly what this is meant to reveal.
      schemaFailures += 1;
    }
  }

  return {
    outputs,
    tokens,
    latencyMs: Date.now() - startedAt,
    schemaFailures,
  };
}

/**
 * How closely two models agree, 0–1.
 *
 * Mean absolute score difference over the postings both scored, rescaled — two
 * models that agree within a few points on every posting are interchangeable
 * for triage, whatever their individual numbers look like.
 */
export function scoreAgreement(
  candidate: ScoredFixture[],
  reference: ScoredFixture[],
): number {
  const byId = new Map(reference.map((item) => [item.jobId, item.score]));

  const diffs = candidate
    .filter((item) => byId.has(item.jobId))
    .map((item) => Math.abs(item.score - byId.get(item.jobId)!));

  if (diffs.length === 0) return 0;

  const mean = diffs.reduce((total, diff) => total + diff, 0) / diffs.length;
  return Math.max(0, 1 - mean / 100);
}
