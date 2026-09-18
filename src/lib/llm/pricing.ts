/**
 * List prices per million tokens, checked 2026-09-17.
 *
 * These exist so the ledger can attribute real dollars per purpose and per
 * model, and so an approval card can quote a cost before the user says yes.
 * Rates change — `scripts/model-bakeoff.ts` is the moment to re-check them, and
 * ledger rows keep the cost computed at call time rather than recomputing from
 * whatever this file says later.
 */

export type Rate = {
  /** USD per million input tokens. */
  input: number;
  /** USD per million output tokens. */
  output: number;
  /**
   * USD per million input tokens served from the provider's prompt cache.
   * Absent means the provider does not discount cached input, so it bills at
   * the normal input rate.
   */
  cachedInput?: number;
  /**
   * USD per million tokens to *write* the cache. Anthropic charges this; the
   * others fold it into the normal input rate.
   */
  cacheWrite?: number;
  /**
   * Set only when a vendor bills reasoning tokens *on top of* output tokens.
   * Every provider here counts them inside `outputTokens` already, so the
   * default is 0 — adding them again would double-count the expensive half of
   * a reasoning model's bill.
   */
  reasoning?: number;
};

/**
 * DeepSeek bills different rates by time of day, in UTC. The off-peak window
 * is a real lever for bulk scoring: a nightly re-score of new postings costs
 * half what the same run costs at midday.
 */
export type TimeBandedRate = {
  bands: Array<{
    /** Inclusive UTC hour the band starts at. */
    fromHourUtc: number;
    /** Exclusive UTC hour the band ends at. Wraps past midnight if smaller. */
    toHourUtc: number;
    rate: Rate;
  }>;
  fallback: Rate;
};

type Pricing = Rate | TimeBandedRate;

function isTimeBanded(pricing: Pricing): pricing is TimeBandedRate {
  return "bands" in pricing;
}

/**
 * Keyed by `provider:model`, the same string shape as the LLM_* env settings.
 * A model missing here costs 0 in the ledger and logs a warning — the ledger
 * row is still written, because a call we cannot price is exactly the one worth
 * noticing.
 */
export const PRICING: Record<string, Pricing> = {
  // --- cheap tier -----------------------------------------------------------
  "deepseek:deepseek-flash": {
    // Off-peak is 16:30–00:30 UTC; we approximate on hour boundaries, which
    // errs toward over-reporting cost rather than under-reporting it.
    bands: [
      {
        fromHourUtc: 17,
        toHourUtc: 0,
        rate: { input: 0.15, output: 0.6, cachedInput: 0.015 },
      },
    ],
    fallback: { input: 0.3, output: 1.2, cachedInput: 0.03 },
  },
  "openai:gpt-5.6-luna": { input: 0.2, output: 1.2, cachedInput: 0.02 },
  "google:gemini-3.8-flash": { input: 0.75, output: 3.75, cachedInput: 0.19 },
  "anthropic:claude-haiku-4-5": {
    input: 1,
    output: 5,
    cachedInput: 0.1,
    cacheWrite: 1.25,
  },

  // --- strong tier ----------------------------------------------------------
  "openai:gpt-5.6-terra": { input: 2, output: 12, cachedInput: 0.2 },
  "google:gemini-3.1-pro-preview": { input: 2, output: 12, cachedInput: 0.5 },
  "anthropic:claude-sonnet-5": {
    input: 2,
    output: 10,
    cachedInput: 0.2,
    cacheWrite: 2.5,
  },
  "anthropic:claude-opus-5": {
    input: 5,
    output: 25,
    cachedInput: 0.5,
    cacheWrite: 6.25,
  },
};

function hourInBand(hour: number, from: number, to: number): boolean {
  return from <= to ? hour >= from && hour < to : hour >= from || hour < to;
}

/** The rate in force for a model right now (or at `at`, for reproducing a row). */
export function rateFor(modelRef: string, at: Date = new Date()): Rate | null {
  const pricing = PRICING[modelRef];
  if (!pricing) return null;
  if (!isTimeBanded(pricing)) return pricing;

  const hour = at.getUTCHours();
  const band = pricing.bands.find((b) =>
    hourInBand(hour, b.fromHourUtc, b.toHourUtc),
  );
  return band?.rate ?? pricing.fallback;
}

export type TokenCounts = {
  /** Input tokens billed at the full rate (cached ones excluded). */
  inputTokens: number;
  outputTokens: number;
  cachedInputTokens?: number;
  /** Tokens written into the provider's cache, where it bills separately. */
  cacheWriteTokens?: number;
  reasoningTokens?: number;
};

/**
 * Cost in USD. Returns 0 for an unpriced model rather than throwing: a missing
 * price must not be able to fail a job that already spent the money.
 */
export function costUsd(
  modelRef: string,
  tokens: TokenCounts,
  at: Date = new Date(),
): number {
  const rate = rateFor(modelRef, at);
  if (!rate) return 0;

  const perMillion = (count: number, price: number) =>
    (count / 1_000_000) * price;

  const cached = tokens.cachedInputTokens ?? 0;
  const cacheWrite = tokens.cacheWriteTokens ?? 0;

  return (
    perMillion(tokens.inputTokens, rate.input) +
    perMillion(cached, rate.cachedInput ?? rate.input) +
    perMillion(cacheWrite, rate.cacheWrite ?? rate.input) +
    perMillion(tokens.outputTokens, rate.output) +
    perMillion(tokens.reasoningTokens ?? 0, rate.reasoning ?? 0)
  );
}

/** Whether we can quote a price for this model, e.g. on an approval card. */
export function isPriced(modelRef: string): boolean {
  return modelRef in PRICING;
}
