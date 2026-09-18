/**
 * The one way this app talks to job boards.
 *
 * Every adapter goes through here so that politeness is not a per-adapter
 * decision: a real User-Agent, a timeout, and backoff that distinguishes "slow
 * down" from "broken". We are a guest on these APIs and they are free, so the
 * default posture is to retreat rather than hammer.
 */

const USER_AGENT =
  "Foray/0.1 (job search agent; +https://github.com/foray) contact: local development";

const DEFAULT_TIMEOUT_MS = 20_000;
const DEFAULT_RETRIES = 3;

export class HttpError extends Error {
  constructor(
    readonly status: number,
    readonly url: string,
    readonly body: string,
  ) {
    super(`HTTP ${status} for ${url}`);
    this.name = "HttpError";
  }
}

/** 429 and 5xx are worth retrying; a 404 means the board is simply not there. */
function isRetryable(status: number): boolean {
  return status === 429 || status === 408 || status >= 500;
}

function backoffMs(attempt: number, retryAfter: string | null): number {
  if (retryAfter) {
    const seconds = Number(retryAfter);
    // Retry-After may be a date rather than a delta; a bad parse falls through.
    if (Number.isFinite(seconds)) return Math.min(seconds * 1000, 60_000);
  }
  // 500ms, 1s, 2s … plus jitter, so parallel fetchers do not resynchronise.
  return 2 ** attempt * 500 + Math.random() * 250;
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

export async function fetchJson<T>(
  url: string,
  options: {
    timeoutMs?: number;
    retries?: number;
    signal?: AbortSignal;
  } = {},
): Promise<T> {
  const text = await fetchText(url, options);
  return JSON.parse(text) as T;
}

export async function fetchText(
  url: string,
  options: {
    timeoutMs?: number;
    retries?: number;
    signal?: AbortSignal;
    accept?: string;
    /** Extra request headers, e.g. an API key. Never logged. */
    headers?: Record<string, string>;
  } = {},
): Promise<string> {
  const retries = options.retries ?? DEFAULT_RETRIES;
  let lastError: unknown;

  for (let attempt = 0; attempt <= retries; attempt += 1) {
    const timeout = AbortSignal.timeout(options.timeoutMs ?? DEFAULT_TIMEOUT_MS);
    const signal = options.signal
      ? AbortSignal.any([timeout, options.signal])
      : timeout;

    try {
      const response = await fetch(url, {
        signal,
        headers: {
          "user-agent": USER_AGENT,
          accept: options.accept ?? "application/json",
          ...options.headers,
        },
      });

      if (response.ok) return response.text();

      const body = (await response.text().catch(() => "")).slice(0, 2000);
      const error = new HttpError(response.status, url, body);

      if (!isRetryable(response.status) || attempt === retries) throw error;

      await sleep(backoffMs(attempt, response.headers.get("retry-after")));
      lastError = error;
      continue;
    } catch (error) {
      // A caller-initiated abort is not a failure to retry through.
      if (options.signal?.aborted) throw error;
      if (error instanceof HttpError) {
        if (!isRetryable(error.status) || attempt === retries) throw error;
      } else if (attempt === retries) {
        throw error;
      } else {
        await sleep(backoffMs(attempt, null));
      }
      lastError = error;
    }
  }

  throw lastError ?? new Error(`Failed to fetch ${url}`);
}
