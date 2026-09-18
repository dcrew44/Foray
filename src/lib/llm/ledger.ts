import type { LanguageModelMiddleware } from "ai";

import { db } from "@/lib/db";
import { llmCalls } from "@/lib/db/schema";
import { currentLlmContext } from "./context";
import { costUsd, type TokenCounts } from "./pricing";
import type { Purpose } from "./models";

/**
 * The cost ledger, as middleware.
 *
 * This was a wrapper function first, and the chat agent quietly slipped past
 * it: `ToolLoopAgent` calls the model from inside the SDK, so a wrapper only
 * the direct call sites used recorded nothing for a whole conversation. As
 * middleware it sits on the model itself, so every path — generateText,
 * streamText, an agent loop, anything added later — is ledgered whether or not
 * its author remembered to.
 */

/**
 * The provider-level usage shape, which differs from the `ai`-level one:
 * counts are nested under `inputTokens` / `outputTokens` rather than flattened
 * onto `*TokenDetails`.
 */
type ProviderUsage = {
  inputTokens?: {
    total?: number;
    noCache?: number;
    cacheRead?: number;
    cacheWrite?: number;
  };
  outputTokens?: { total?: number; text?: number; reasoning?: number };
};

export function tokensFromProviderUsage(
  usage: ProviderUsage | undefined,
): TokenCounts {
  const cacheRead = usage?.inputTokens?.cacheRead ?? 0;
  const cacheWrite = usage?.inputTokens?.cacheWrite ?? 0;
  // A provider that reports only a total still gets its uncached share right.
  const noCache =
    usage?.inputTokens?.noCache ??
    Math.max((usage?.inputTokens?.total ?? 0) - cacheRead, 0);

  return {
    inputTokens: noCache,
    outputTokens: usage?.outputTokens?.total ?? 0,
    cachedInputTokens: cacheRead,
    cacheWriteTokens: cacheWrite,
    reasoningTokens: usage?.outputTokens?.reasoning ?? 0,
  };
}

async function write(input: {
  purpose: Purpose;
  ref: string;
  tokens: TokenCounts;
  latencyMs: number;
  error?: string;
}) {
  const { userId, runId } = currentLlmContext();
  const [provider, ...rest] = input.ref.split(":");

  try {
    await db.insert(llmCalls).values({
      userId: userId ?? null,
      runId: runId ?? null,
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

/** Middleware that ledgers one model, for one purpose. */
export function ledgerMiddleware(
  purpose: Purpose,
  ref: string,
): LanguageModelMiddleware {
  return {
    async wrapGenerate({ doGenerate }) {
      const startedAt = Date.now();
      try {
        const result = await doGenerate();
        await write({
          purpose,
          ref,
          tokens: tokensFromProviderUsage(result.usage as ProviderUsage),
          latencyMs: Date.now() - startedAt,
        });
        return result;
      } catch (error) {
        await write({
          purpose,
          ref,
          tokens: { inputTokens: 0, outputTokens: 0 },
          latencyMs: Date.now() - startedAt,
          error: error instanceof Error ? error.message : String(error),
        });
        throw error;
      }
    },

    async wrapStream({ doStream }) {
      const startedAt = Date.now();

      let result;
      try {
        result = await doStream();
      } catch (error) {
        await write({
          purpose,
          ref,
          tokens: { inputTokens: 0, outputTokens: 0 },
          latencyMs: Date.now() - startedAt,
          error: error instanceof Error ? error.message : String(error),
        });
        throw error;
      }

      // Usage only arrives at the end of a stream, so the row is written when
      // the `finish` part goes past rather than when the call returns.
      let usage: ProviderUsage | undefined;
      let recorded = false;

      const recordOnce = async (error?: string) => {
        if (recorded) return;
        recorded = true;
        await write({
          purpose,
          ref,
          tokens: tokensFromProviderUsage(usage),
          latencyMs: Date.now() - startedAt,
          error,
        });
      };

      return {
        ...result,
        stream: result.stream.pipeThrough(
          new TransformStream({
            transform(chunk, controller) {
              if (chunk.type === "finish") {
                usage = chunk.usage as ProviderUsage;
              }
              controller.enqueue(chunk);
            },
            // Covers a stream that ends without a finish part — an aborted
            // request still cost whatever the provider had already produced.
            async flush() {
              await recordOnce();
            },
          }),
        ),
      };
    },
  };
}
