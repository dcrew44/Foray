import {
  NoObjectGeneratedError,
  NoOutputGeneratedError,
  Output,
  TypeValidationError,
  type LanguageModelUsage,
} from "ai";
import type { z } from "zod";

import { callLLM, type CallContext, type CallOverrides } from "./call";
import type { Purpose } from "./models";

/**
 * Structured output with one retry on a malformed response.
 *
 * Cheap models occasionally emit JSON that does not match the schema. That is a
 * modelling failure, not a transport failure, so the SDK's `maxRetries` does
 * not cover it — it only retries the call setup. One retry is worth it; a
 * second usually means the schema or the prompt is wrong, and looping would
 * quietly multiply the bill.
 */

export type StructuredArgs = Omit<
  Parameters<typeof callLLM>[1],
  "output" | "model"
>;

export type StructuredResult<T> = {
  value: T;
  usage: LanguageModelUsage;
  /** True when the first attempt produced unusable output. */
  retried: boolean;
};

/**
 * Failures worth one more attempt.
 *
 * Two distinct ways a model can fail to produce usable structured output:
 *
 * - It wrote JSON that does not match the schema. Modelling failure, not
 *   transport, so the SDK's `maxRetries` does not cover it.
 * - It produced no output at all. Providers without native JSON-schema support
 *   get the schema injected into the system message instead, and occasionally
 *   answer with reasoning and no object. Seen in practice on DeepSeek, which is
 *   exactly the cheap-tier model the bulk passes use.
 *
 * Both are worth exactly one retry. A second failure usually means the schema
 * or the prompt is wrong, and looping would quietly multiply the bill.
 */
function isRetryableOutputFailure(error: unknown): boolean {
  if (NoOutputGeneratedError.isInstance(error)) return true;

  if (!NoObjectGeneratedError.isInstance(error)) return false;
  // A `length` finish means the response was truncated: retrying the same
  // request just truncates again, so treat it as a real failure.
  if (error.finishReason === "length") return false;
  return (
    TypeValidationError.isInstance(error.cause) ||
    error.cause instanceof SyntaxError
  );
}

export async function generateStructured<T>(
  purpose: Purpose,
  schema: z.ZodType<T>,
  args: StructuredArgs,
  context: CallContext = {},
  overrides: CallOverrides = {},
): Promise<StructuredResult<T>> {
  const attempt = async () => {
    const result = await callLLM(
      purpose,
      { ...args, output: Output.object({ schema }) },
      context,
      overrides,
    );
    // `output` is a getter that throws when the response did not parse, so it
    // must be read inside the try rather than returned lazily.
    return { value: result.output as T, usage: result.usage };
  };

  try {
    return { ...(await attempt()), retried: false };
  } catch (error) {
    if (!isRetryableOutputFailure(error)) throw error;
    console.warn(
      `[llm] ${purpose}: unusable structured output (${
        error instanceof Error ? error.name : "unknown"
      }), retrying once`,
    );
    return { ...(await attempt()), retried: true };
  }
}
