import { describe, expect, it } from "vitest";

import { costUsd, isPriced, rateFor } from "@/lib/llm/pricing";

/** 16:00 UTC — inside DeepSeek's peak window. */
const peak = new Date("2026-09-18T16:00:00Z");
/** 20:00 UTC — inside the off-peak window. */
const offPeak = new Date("2026-09-18T20:00:00Z");
/** 02:00 UTC — off-peak, but only if the band wraps past midnight. */
const afterMidnight = new Date("2026-09-18T02:00:00Z");

describe("rateFor", () => {
  it("returns null for a model we have no price for", () => {
    expect(rateFor("openai:some-unreleased-model")).toBeNull();
    expect(isPriced("openai:some-unreleased-model")).toBe(false);
  });

  it("picks DeepSeek's band by UTC hour", () => {
    expect(rateFor("deepseek:deepseek-flash", peak)?.input).toBe(0.3);
    expect(rateFor("deepseek:deepseek-flash", offPeak)?.input).toBe(0.15);
  });

  it("treats a band that wraps past midnight as one window", () => {
    // 17:00–00:00 does not contain 02:00, so this must fall back to peak.
    expect(rateFor("deepseek:deepseek-flash", afterMidnight)?.input).toBe(0.3);
  });

  it("returns a flat rate unchanged whatever the hour", () => {
    expect(rateFor("openai:gpt-5.6-luna", peak)).toEqual(
      rateFor("openai:gpt-5.6-luna", offPeak),
    );
  });
});

describe("costUsd", () => {
  it("bills input and output at their own rates", () => {
    // 1M in at $0.20 + 1M out at $1.20.
    expect(
      costUsd("openai:gpt-5.6-luna", {
        inputTokens: 1_000_000,
        outputTokens: 1_000_000,
      }),
    ).toBeCloseTo(1.4, 10);
  });

  it("bills cached input at the cached rate, not the full one", () => {
    const uncached = costUsd("openai:gpt-5.6-luna", {
      inputTokens: 1_000_000,
      outputTokens: 0,
    });
    const cached = costUsd("openai:gpt-5.6-luna", {
      inputTokens: 0,
      outputTokens: 0,
      cachedInputTokens: 1_000_000,
    });
    expect(uncached).toBeCloseTo(0.2, 10);
    expect(cached).toBeCloseTo(0.02, 10);
  });

  it("charges Anthropic's cache writes at the write rate", () => {
    expect(
      costUsd("anthropic:claude-haiku-4-5", {
        inputTokens: 0,
        outputTokens: 0,
        cacheWriteTokens: 1_000_000,
      }),
    ).toBeCloseTo(1.25, 10);
  });

  it("does not double-count reasoning tokens", () => {
    // Providers already count reasoning inside outputTokens, so passing them
    // must not add a second charge.
    const withReasoning = costUsd("openai:gpt-5.6-terra", {
      inputTokens: 0,
      outputTokens: 1_000_000,
      reasoningTokens: 500_000,
    });
    expect(withReasoning).toBeCloseTo(12, 10);
  });

  it("costs an unpriced model at zero rather than throwing", () => {
    expect(
      costUsd("compat:whatever", { inputTokens: 5_000, outputTokens: 5_000 }),
    ).toBe(0);
  });

  it("makes DeepSeek's off-peak window materially cheaper", () => {
    const tokens = { inputTokens: 2_000_000, outputTokens: 200_000 };
    const dayRate = costUsd("deepseek:deepseek-flash", tokens, peak);
    const nightRate = costUsd("deepseek:deepseek-flash", tokens, offPeak);
    expect(nightRate).toBeCloseTo(dayRate / 2, 10);
  });
});
