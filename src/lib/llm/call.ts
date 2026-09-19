import { generateText } from "ai";

import { acquire } from "./limiter";
import { withLlmContext, type LlmContext } from "./context";
import {
  model,
  modelRef,
  providerOf,
  quirksFor,
  reasoningFor,
  type Purpose,
} from "./models";

/**
 * A direct, non-agent call to a purpose's model.
 *
 * The ledger is not written here — it lives in middleware on the model itself
 * (see ledger.ts), so agent loops and anything else that reaches the model from
 * inside the SDK are recorded too. What this adds is the things a caller
 * genuinely chooses: waiting for the provider's rate-limit slot, the portable
 * reasoning effort for the purpose, and the attribution context the ledger
 * reads back out.
 */

export type { LlmContext as CallContext };

type GenerateArgs = Omit<
  Parameters<typeof generateText>[0],
  "model" | "reasoning"
>;

export type CallOverrides = {
  /** Used by the bake-off to compare the same prompt across models. */
  model?: Parameters<typeof generateText>[0]["model"];
  reasoning?: Parameters<typeof generateText>[0]["reasoning"];
  providerId?: string;
};

export async function callLLM<T extends GenerateArgs>(
  purpose: Purpose,
  args: T,
  context: LlmContext = {},
  overrides: CallOverrides = {},
) {
  const ref = modelRef(purpose);
  const quirks = quirksFor(ref);

  await acquire(overrides.providerId ?? providerOf(purpose));

  return withLlmContext(context, () =>
    generateText({
      ...args,
      model: overrides.model ?? model(purpose),
      reasoning: overrides.reasoning ?? reasoningFor(purpose),
      // Some vendors reject an explicit temperature on reasoning models rather
      // than ignoring it, so it is dropped instead of passed through.
      ...(quirks.supportsTemperature ? {} : { temperature: undefined }),
    } as Parameters<typeof generateText>[0]),
  );
}
