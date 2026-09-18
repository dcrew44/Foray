import {
  NoObjectGeneratedError,
  Output,
  TypeValidationError,
  type LanguageModelUsage,
} from "ai";
import type { z } from "zod";

import { callLLM, type CallContext } from "./call";
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

/** Distinguishes "the model wrote bad JSON" from "the call itself failed". */
function isSchemaFailure(error: unknown): boolean {
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
): Promise<StructuredResult<T>> {
  const attempt = async () => {
    const result = await callLLM(
      purpose,
      { ...args, output: Output.object({ schema }) },
      context,
    );
    // `output` is a getter that throws when the response did not parse, so it
    // must be read inside the try rather than returned lazily.
    return { value: result.output as T, usage: result.usage };
  };

  try {
    return { ...(await attempt()), retried: false };
  } catch (error) {
    if (!isSchemaFailure(error)) throw error;
    console.warn(
      `[llm] ${purpose}: response did not match schema, retrying once`,
    );
    return { ...(await attempt()), retried: true };
  }
}
