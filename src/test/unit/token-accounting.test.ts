import { describe, expect, it } from "vitest";
import type { LanguageModelUsage } from "ai";

import { tokensFrom } from "@/lib/llm/call";
import { costUsd } from "@/lib/llm/pricing";

/**
 * AI SDK 7 normalizes cached/uncached token reporting across vendors onto
 * `usage.inputTokenDetails`, which is a different shape from earlier versions.
 * The ledger's cost attribution depends on reading it correctly, so it is
 * pinned here.
 */

function usage(partial: Partial<LanguageModelUsage>): LanguageModelUsage {
  return {
    inputTokens: undefined,
    inputTokenDetails: {
      noCacheTokens: undefined,
      cacheReadTokens: undefined,
      cacheWriteTokens: undefined,
    },
    outputTokens: undefined,
    outputTokenDetails: { textTokens: undefined, reasoningTokens: undefined },
    totalTokens: undefined,
    ...partial,
  } as LanguageModelUsage;
}

describe("tokensFrom", () => {
  it("splits cached from uncached input", () => {
    const tokens = tokensFrom(
      usage({
        inputTokens: 1000,
        inputTokenDetails: {
          noCacheTokens: 400,
          cacheReadTokens: 600,
          cacheWriteTokens: undefined,
        },
        outputTokens: 120,
      }),
    );

    expect(tokens.inputTokens).toBe(400);
    expect(tokens.cachedInputTokens).toBe(600);
    expect(tokens.outputTokens).toBe(120);
  });

  it("derives uncached input when a provider reports no detail", () => {
    const tokens = tokensFrom(
      usage({
        inputTokens: 1000,
        inputTokenDetails: {
          noCacheTokens: undefined,
          cacheReadTokens: 250,
          cacheWriteTokens: undefined,
        },
      }),
    );

    expect(tokens.inputTokens).toBe(750);
    expect(tokens.cachedInputTokens).toBe(250);
  });

  it("never reports negative input if the totals disagree", () => {
    const tokens = tokensFrom(
      usage({
        inputTokens: 100,
        inputTokenDetails: {
          noCacheTokens: undefined,
          cacheReadTokens: 400,
          cacheWriteTokens: undefined,
        },
      }),
    );

    expect(tokens.inputTokens).toBe(0);
  });

  it("carries Anthropic's cache writes through to cost", () => {
    const tokens = tokensFrom(
      usage({
        inputTokens: 1000,
        inputTokenDetails: {
          noCacheTokens: 0,
          cacheReadTokens: 1000,
          cacheWriteTokens: 1_000_000,
        },
      }),
    );

    expect(tokens.cacheWriteTokens).toBe(1_000_000);
    // Cache writes bill at $1.25/M on Haiku 4.5, cache reads at $0.10/M.
    expect(costUsd("anthropic:claude-haiku-4-5", tokens)).toBeCloseTo(
      1.25 + 0.0001,
      6,
    );
  });

  it("keeps reasoning tokens visible without charging them twice", () => {
    const tokens = tokensFrom(
      usage({
        outputTokens: 900,
        outputTokenDetails: { textTokens: 400, reasoningTokens: 500 },
      }),
    );

    expect(tokens.reasoningTokens).toBe(500);
    expect(tokens.outputTokens).toBe(900);
  });

  it("treats a missing usage object as a zero-cost row rather than throwing", () => {
    const tokens = tokensFrom(undefined);
    expect(tokens).toMatchObject({ inputTokens: 0, outputTokens: 0 });
    expect(costUsd("openai:gpt-5.6-luna", tokens)).toBe(0);
  });
});
