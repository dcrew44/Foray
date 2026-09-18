import "@/lib/load-dotenv";

import { z } from "zod";

import { pool } from "@/lib/db";
import { configuredPurposes, type Purpose } from "@/lib/llm/models";
import { configuredProviders } from "@/lib/llm/registry";
import { isPriced, rateFor } from "@/lib/llm/pricing";
import { generateStructured } from "@/lib/llm/structured";

/**
 * Proves the LLM layer end to end: every configured purpose reaches its
 * provider, returns schema-valid output, and leaves a priced ledger row.
 *
 *   pnpm tsx scripts/llm-smoke.ts
 *   pnpm tsx scripts/llm-smoke.ts score chat
 */

const Answer = z.object({
  city: z.string().describe("The city named in the question."),
  confident: z.boolean(),
});

async function main() {
  const providers = configuredProviders();
  console.log(
    providers.length
      ? `providers with credentials: ${providers.join(", ")}`
      : "no provider credentials found — set one in .env (see .env.example)",
  );

  const requested = process.argv.slice(2) as Purpose[];
  const purposes = configuredPurposes().filter(
    (p) => requested.length === 0 || requested.includes(p.purpose),
  );

  let failures = 0;

  for (const { purpose, ref, error } of purposes) {
    if (!ref) {
      console.log(`✗ ${purpose.padEnd(9)} ${error}`);
      failures += 1;
      continue;
    }

    const priced = isPriced(ref) ? "" : "  (unpriced — add it to pricing.ts)";
    const startedAt = Date.now();

    try {
      const { value, usage, retried } = await generateStructured(
        purpose,
        Answer,
        {
          instructions:
            "Answer factual questions. Respond only with the requested fields.",
          prompt: "Which city is the Golden Gate Bridge in?",
        },
      );

      const rate = rateFor(ref);
      console.log(
        `✓ ${purpose.padEnd(9)} ${ref.padEnd(32)} ${Date.now() - startedAt}ms  ` +
          `in=${usage.inputTokens ?? 0} ` +
          `cached=${usage.inputTokenDetails?.cacheReadTokens ?? 0} ` +
          `out=${usage.outputTokens ?? 0}` +
          `${retried ? "  (retried)" : ""}` +
          `${rate ? "" : priced}` +
          `  → ${JSON.stringify(value)}`,
      );
    } catch (callError) {
      failures += 1;
      console.log(
        `✗ ${purpose.padEnd(9)} ${ref}: ${
          callError instanceof Error ? callError.message : String(callError)
        }`,
      );
    }
  }

  console.log(
    failures === 0
      ? "\nall configured purposes answered. Check the ledger:\n" +
          "  select purpose, model, input_tokens, cached_input_tokens, output_tokens, cost_usd from llm_calls order by created_at desc;"
      : `\n${failures} purpose(s) failed.`,
  );

  await pool.end();
  process.exit(failures === 0 ? 0 : 1);
}

void main();
