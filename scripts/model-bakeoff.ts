import "@/lib/load-dotenv";

import { pool } from "@/lib/db";
import { costUsd, isPriced, type TokenCounts } from "@/lib/llm/pricing";
import type { Purpose } from "@/lib/llm/models";

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
};

export type FixtureRun<TOutput> = {
  /** Runs the purpose's real prompt against one model, without the ledger's purpose routing. */
  run: (modelRef: string) => Promise<{
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
  // score:    → Step 16 (20 postings against a fixed profile)
  // extract:  → Step 8  (3 resumes: single column, two column, LaTeX export)
  // tailor:   → Step 20 (3 job/resume pairs, scored by the verifier)
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

  return { purpose, models, reference: get("--reference") };
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
    ? (await fixture.run(args.reference)).outputs
    : null;

  const rows: Array<Record<string, string>> = [];

  for (const modelRef of args.models) {
    const result = await fixture.run(modelRef);
    rows.push({
      model: modelRef,
      cost: `$${costUsd(modelRef, result.tokens).toFixed(4)}`,
      latency: `${result.latencyMs}ms`,
      schemaFail: `${result.schemaFailures}/${result.outputs.length + result.schemaFailures}`,
      agreement: referenceOutputs
        ? fixture.agreement(result.outputs, referenceOutputs).toFixed(2)
        : "—",
    });
  }

  console.table(rows);
  await pool.end();
}

void main();
