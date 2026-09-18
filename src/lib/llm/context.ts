import { AsyncLocalStorage } from "node:async_hooks";

/**
 * Who a model call belongs to.
 *
 * The purpose is known where the model is built, but the user and run are not:
 * an agent loop calls the model from inside the SDK, several frames below any
 * code of ours. Async-local storage carries that attribution down without
 * threading it through APIs we do not own.
 */

export type LlmContext = {
  userId?: string | null;
  runId?: string | null;
};

const storage = new AsyncLocalStorage<LlmContext>();

/** Runs `fn` with every model call inside it attributed to this context. */
export function withLlmContext<T>(context: LlmContext, fn: () => T): T {
  return storage.run(context, fn);
}

export function currentLlmContext(): LlmContext {
  return storage.getStore() ?? {};
}
