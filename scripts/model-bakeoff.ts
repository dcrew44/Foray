import "@/lib/load-dotenv";

import { pool } from "@/lib/db";
import { costUsd, isPriced, type TokenCounts } from "@/lib/llm/pricing";
import type { Purpose, ReasoningEffort } from "@/lib/llm/models";
import {
  runScoringFixture,
  scoreAgreement,
} from "@/lib/ranking/bakeoff-fixture";

/**
 * Compare models for one purpose on fixed fixtures.
 *
 * Which model should score five hundred postings is a question about money and
 * reliability, not taste, so it gets answered with numbers: cost per run,
 * latency, how often a model returns output the schema rejects, and how far it
 * drifts from a reference model we trust. The fixtures are filled in as the
 * real prompts land — scoring in Step 16, extraction in Step 8, tailoring in
 * Step 20 — so this skeleton exists now to be extended, not rewritten.
 *
 *   pnpm llm:bakeoff --purpose score --models deepseek:deepseek-flash,openai:gpt-5.6-luna
 *   pnpm llm:bakeoff --purpose score --models ... --reference anthropic:claude-sonnet-5
 */

type Args = {
  purpose: Purpose;
  models: string[];
  reference?: string;
  /**
   * Compare at one effort level. Worth varying on its own: on DeepSeek Flash,
   * reasoning is 92% of a scoring call's output tokens, so effort moves the
   * bill more than the choice of model does.
   */
  reasoning?: ReasoningEffort;
};

export type FixtureRun<TOutput> = {
  /** Runs the purpose's real prompt against one model, without the ledger's purpose routing. */
  run: (
    modelRef: string,
    options?: { reasoning?: ReasoningEffort },
  ) => Promise<{
    outputs: TOutput[];
    tokens: TokenCounts;
    latencyMs: number;
    schemaFailures: number;
  }>;
  /** 0–1: how closely a candidate's outputs track the reference's. */
  agreement: (candidate: TOutput[], reference: TOutput[]) => number;
};

/**
 * Filled in per purpose as each prompt is written. Keeping the registry here
 * rather than inside each prompt module means one place answers "what can be
 * baked off yet".
 */
export const FIXTURES: Partial<Record<Purpose, FixtureRun<unknown>>> = {
  score: {
    run: async (modelRef, options) => {
      const result = await runScoringFixture(modelRef, {
        reasoning: options?.reasoning,
      });
      return {
        outputs: result.outputs,
        tokens: result.tokens,
        latencyMs: result.latencyMs,
        schemaFailures: result.schemaFailures,
      };
    },
    agreement: (candidate, reference) =>
      scoreAgreement(
        candidate as Array<{ jobId: string; score: number }>,
        reference as Array<{ jobId: string; score: number }>,
      ),
  } as FixtureRun<unknown>,
  // extract: → resumes (single column, two column, LaTeX export)
  // tailor:  → Step 20 (3 job/resume pairs, scored by the verifier)
};

function parseArgs(argv: string[]): Args {
  const get = (flag: string) => {
    const index = argv.indexOf(flag);
    return index === -1 ? undefined : argv[index + 1];
  };

  const purpose = get("--purpose") as Purpose | undefined;
  const models = get("--models")?.split(",").filter(Boolean) ?? [];

  if (!purpose || models.length === 0) {
    console.error(
      "usage: pnpm llm:bakeoff --purpose <purpose> --models a:b,c:d [--reference a:b]",
    );
    process.exit(2);
  }

  return {
    purpose,
    models,
    reference: get("--reference"),
    reasoning: get("--reasoning") as ReasoningEffort | undefined,
  };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));

  const unpriced = args.models.filter((m) => !isPriced(m));
  if (unpriced.length) {
    console.warn(
      `warning: no price for ${unpriced.join(", ")} — their cost column will read 0. ` +
        "Add them to src/lib/llm/pricing.ts.",
    );
  }

  const fixture = FIXTURES[args.purpose];
  if (!fixture) {
    console.error(
      `No fixtures for "${args.purpose}" yet. They land with that purpose's prompt; ` +
        "see the comments in FIXTURES.",
    );
    await pool.end();
    process.exit(1);
  }

  const referenceOutputs = args.reference
    ? (await fixture.run(args.reference, { reasoning: args.reasoning })).outputs
    : null;

  const rows: Array<Record<string, string>> = [];

  for (const modelRef of args.models) {
    const result = await fixture.run(modelRef, { reasoning: args.reasoning });
    const total = result.outputs.length + result.schemaFailures;
    const cost = costUsd(modelRef, result.tokens);

    rows.push({
      model: modelRef,
      cost: `$${cost.toFixed(4)}`,
      // The number that actually decides: one search scores hundreds.
      per500: `$${(total > 0 ? (cost / total) * 500 : 0).toFixed(2)}`,
      latency: `${(result.latencyMs / 1000).toFixed(1)}s`,
      reasoningShare: `${Math.round(((result.tokens.reasoningTokens ?? 0) / Math.max(result.tokens.outputTokens, 1)) * 100)}%`,
      schemaFail: `${result.schemaFailures}/${total}`,
      agreement: referenceOutputs
        ? fixture.agreement(result.outputs, referenceOutputs).toFixed(2)
        : "—",
    });
  }

  console.table(rows);
  await pool.end();
}

void main();
