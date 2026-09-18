import { env } from "@/env";

/**
 * A per-provider request-rate ceiling.
 *
 * Scoring a few hundred postings fires calls as fast as `p-limit` allows, which
 * is well past what a new provider account is allowed. Waiting our turn here is
 * cheaper than a 429 storm and its retries, and it keeps one busy pipeline from
 * starving another: the bucket is per provider, so a DeepSeek scoring run does
 * not consume the chat model's headroom.
 */

const DEFAULT_RPM = 60;

class TokenBucket {
  private tokens: number;
  private lastRefill = Date.now();

  constructor(private readonly ratePerMinute: number) {
    this.tokens = ratePerMinute;
  }

  private refill() {
    const now = Date.now();
    const elapsedMinutes = (now - this.lastRefill) / 60_000;
    if (elapsedMinutes <= 0) return;
    this.tokens = Math.min(
      this.ratePerMinute,
      this.tokens + elapsedMinutes * this.ratePerMinute,
    );
    this.lastRefill = now;
  }

  /** Resolves when a slot is free. Serialized per provider by the queue below. */
  async take(): Promise<void> {
    for (;;) {
      this.refill();
      if (this.tokens >= 1) {
        this.tokens -= 1;
        return;
      }
      const deficit = 1 - this.tokens;
      const waitMs = Math.ceil((deficit / this.ratePerMinute) * 60_000);
      await new Promise((resolve) => setTimeout(resolve, Math.max(waitMs, 25)));
    }
  }
}

const RPM: Record<string, number | undefined> = {
  openai: env.LLM_RPM_OPENAI,
  google: env.LLM_RPM_GOOGLE,
  deepseek: env.LLM_RPM_DEEPSEEK,
  anthropic: env.LLM_RPM_ANTHROPIC,
  compat: env.LLM_RPM_COMPAT,
};

const buckets = new Map<string, TokenBucket>();
/** One waiter chain per provider, so concurrent callers queue rather than race. */
const tails = new Map<string, Promise<void>>();

function bucketFor(provider: string): TokenBucket {
  let bucket = buckets.get(provider);
  if (!bucket) {
    bucket = new TokenBucket(RPM[provider] ?? DEFAULT_RPM);
    buckets.set(provider, bucket);
  }
  return bucket;
}

/** Waits for a slot on `provider`'s bucket. Every LLM call goes through here. */
export function acquire(provider: string): Promise<void> {
  const previous = tails.get(provider) ?? Promise.resolve();
  const next = previous.then(() => bucketFor(provider).take());
  // Keep the chain alive even if a waiter's caller later rejects.
  tails.set(
    provider,
    next.catch(() => undefined),
  );
  return next;
}
