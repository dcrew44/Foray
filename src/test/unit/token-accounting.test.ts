import { describe, expect, it } from "vitest";

import { tokensFromProviderUsage } from "@/lib/llm/ledger";
import { costUsd } from "@/lib/llm/pricing";

/**
 * The ledger reads usage at the *provider* level, where counts are nested
 * under inputTokens/outputTokens rather than flattened onto the `ai`-level
 * *TokenDetails fields. Cost attribution depends on reading that shape
 * correctly, so it is pinned here.
 */

type Usage = Parameters<typeof tokensFromProviderUsage>[0];

function usage(partial: {
  input?: Partial<NonNullable<NonNullable<Usage>["inputTokens"]>>;
  output?: Partial<NonNullable<NonNullable<Usage>["outputTokens"]>>;
}): Usage {
  return {
    inputTokens: partial.input ?? {},
    outputTokens: partial.output ?? {},
  };
}

describe("tokensFromProviderUsage", () => {
  it("splits cached from uncached input", () => {
    const tokens = tokensFromProviderUsage(
      usage({
        input: { total: 1000, noCache: 400, cacheRead: 600 },
        output: { total: 120 },
      }),
    );

    expect(tokens.inputTokens).toBe(400);
    expect(tokens.cachedInputTokens).toBe(600);
    expect(tokens.outputTokens).toBe(120);
  });

  it("derives uncached input when a provider reports only a total", () => {
    const tokens = tokensFromProviderUsage(
      usage({ input: { total: 1000, cacheRead: 250 } }),
    );

    expect(tokens.inputTokens).toBe(750);
    expect(tokens.cachedInputTokens).toBe(250);
  });

  it("never reports negative input if the totals disagree", () => {
    const tokens = tokensFromProviderUsage(
      usage({ input: { total: 100, cacheRead: 400 } }),
    );

    expect(tokens.inputTokens).toBe(0);
  });

  it("carries Anthropic's cache writes through to cost", () => {
    const tokens = tokensFromProviderUsage(
      usage({
        input: { total: 1000, noCache: 0, cacheRead: 1000, cacheWrite: 1_000_000 },
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
    const tokens = tokensFromProviderUsage(
      usage({ output: { total: 900, text: 400, reasoning: 500 } }),
    );

    expect(tokens.reasoningTokens).toBe(500);
    expect(tokens.outputTokens).toBe(900);
    // 900 output tokens on Terra at $12/M — the 500 reasoning tokens are
    // already inside that total and must not add a second charge.
    expect(costUsd("openai:gpt-5.6-terra", tokens)).toBeCloseTo(
      (900 / 1_000_000) * 12,
      10,
    );
  });

  it("treats a missing usage object as a zero-cost row rather than throwing", () => {
    const tokens = tokensFromProviderUsage(undefined);
    expect(tokens).toMatchObject({ inputTokens: 0, outputTokens: 0 });
    expect(costUsd("openai:gpt-5.6-luna", tokens)).toBe(0);
  });
});
