import { generateText, type LanguageModelUsage } from "ai";

import { db } from "@/lib/db";
import { llmCalls } from "@/lib/db/schema";
import { costUsd, type TokenCounts } from "./pricing";
import { acquire } from "./limiter";
import {
  model,
  modelRef,
  providerOf,
  quirksFor,
  reasoningFor,
  type Purpose,
} from "./models";

/**
 * The single door to every provider.
 *
 * Nothing calls `generateText` directly: this wrapper waits for the provider's
 * rate-limit slot, runs the call, and writes an `llm_calls` row whether it
 * succeeded or failed. That ledger is how we answer "which model should do
 * this" with measurements instead of guesses, and it is the seed of usage
 * billing later. A call with no row is a cost nobody can see.
 */

export type CallContext = {
  userId?: string | null;
  runId?: string | null;
};

/**
 * AI SDK 7 reports token details in a normalized shape across vendors, so the
 * ledger does not need per-provider branching:
 *
 *   usage.inputTokens                       total in, cached included
 *   usage.inputTokenDetails.noCacheTokens   billed at the full input rate
 *   usage.inputTokenDetails.cacheReadTokens served from the provider's cache
 *   usage.inputTokenDetails.cacheWriteTokens written to it (Anthropic bills this)
 *   usage.outputTokenDetails.reasoningTokens counted inside outputTokens already
 */
export function tokensFrom(usage: LanguageModelUsage | undefined): TokenCounts {
  const cacheRead = usage?.inputTokenDetails?.cacheReadTokens ?? 0;
  const cacheWrite = usage?.inputTokenDetails?.cacheWriteTokens ?? 0;
  // Fall back to the total minus cached, for a provider that reports no detail.
  const noCache =
    usage?.inputTokenDetails?.noCacheTokens ??
    Math.max((usage?.inputTokens ?? 0) - cacheRead, 0);

  return {
    inputTokens: noCache,
    outputTokens: usage?.outputTokens ?? 0,
    cachedInputTokens: cacheRead,
    cacheWriteTokens: cacheWrite,
    reasoningTokens: usage?.outputTokenDetails?.reasoningTokens ?? 0,
  };
}

async function record(input: {
  purpose: Purpose;
  ref: string;
  context: CallContext;
  tokens: TokenCounts;
  latencyMs: number;
  error?: string;
}) {
  const [provider, ...rest] = input.ref.split(":");
  try {
    await db.insert(llmCalls).values({
      userId: input.context.userId ?? null,
      runId: input.context.runId ?? null,
      purpose: input.purpose,
      provider,
      model: rest.join(":"),
      inputTokens: input.tokens.inputTokens,
      outputTokens: input.tokens.outputTokens,
      cachedInputTokens: input.tokens.cachedInputTokens ?? 0,
      reasoningTokens: input.tokens.reasoningTokens ?? 0,
      costUsd: costUsd(input.ref, input.tokens).toFixed(6),
      latencyMs: input.latencyMs,
      error: input.error ?? null,
    });
  } catch (error) {
    // Losing a ledger row must not fail work that already spent the money.
    console.error("[llm] failed to write ledger row:", error);
  }
}

type GenerateArgs = Omit<
  Parameters<typeof generateText>[0],
  "model" | "reasoning"
>;

/**
 * Run a purpose against its configured model, ledger included.
 *
 * `reasoning` is the AI SDK's portable effort setting, so this stays
 * vendor-neutral; per-vendor oddities live in models.ts, not here.
 */
export async function callLLM<T extends GenerateArgs>(
  purpose: Purpose,
  args: T,
  context: CallContext = {},
) {
  const ref = modelRef(purpose);
  const provider = providerOf(purpose);
  const quirks = quirksFor(ref);

  await acquire(provider);

  const startedAt = Date.now();
  try {
    const result = await generateText({
      ...args,
      model: model(purpose),
      reasoning: reasoningFor(purpose),
      // Some vendors reject an explicit temperature on reasoning models rather
      // than ignoring it, so it is dropped instead of being passed through.
      ...(quirks.supportsTemperature ? {} : { temperature: undefined }),
    } as Parameters<typeof generateText>[0]);

    await record({
      purpose,
      ref,
      context,
      tokens: tokensFrom(result.usage),
      latencyMs: Date.now() - startedAt,
    });

    return result;
  } catch (error) {
    await record({
      purpose,
      ref,
      context,
      tokens: {
        inputTokens: 0,
        outputTokens: 0,
      },
      latencyMs: Date.now() - startedAt,
      error: error instanceof Error ? error.message : String(error),
    });
    throw error;
  }
}
