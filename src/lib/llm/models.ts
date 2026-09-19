import { wrapLanguageModel, type LanguageModel } from "ai";

import { env } from "@/env";
import { ledgerMiddleware } from "./ledger";
import {
  PROVIDER_KEY_ENV,
  hasCredential,
  registry,
  type ProviderId,
} from "./registry";

/**
 * Purpose in, model out.
 *
 * Call sites ask for what they are doing — `model("score")` — never for a
 * vendor. This is the only file that knows model names or vendor quirks, so
 * changing which model scores postings is an .env edit, and working around a
 * provider's oddity is a change here rather than in a dozen call sites.
 */

export type Purpose =
  | "chat"
  | "extract"
  | "score"
  | "evaluate"
  | "tailor"
  | "verify"
  | "merge";

export const PURPOSES: Purpose[] = [
  "chat",
  "extract",
  "score",
  "evaluate",
  "tailor",
  "verify",
  "merge",
];

/** Purposes that read page images when a PDF's text layer is unusable. */
export const VISION_PURPOSES: ReadonlySet<Purpose> = new Set([
  "extract",
  "merge",
]);

const PURPOSE_ENV: Record<Purpose, string | undefined> = {
  chat: env.LLM_CHAT,
  extract: env.LLM_EXTRACT,
  score: env.LLM_SCORE,
  evaluate: env.LLM_EVALUATE,
  tailor: env.LLM_TAILOR,
  verify: env.LLM_VERIFY,
  merge: env.LLM_MERGE,
};

/**
 * How hard the model should think, per purpose.
 *
 * The AI SDK's top-level `reasoning` setting is portable across OpenAI,
 * Anthropic, Google and DeepSeek, so this stays vendor-neutral. Tailoring gets
 * the most because a fabricated bullet is the worst failure mode in the
 * product; bulk passes get the least because they run hundreds of times.
 */
const REASONING_DEFAULTS: Record<Purpose, ReasoningEffort> = {
  chat: "low",
  extract: "low",
  score: "minimal",
  evaluate: "medium",
  tailor: "high",
  verify: "low",
  merge: "low",
};

export type ReasoningEffort =
  | "none"
  | "minimal"
  | "low"
  | "medium"
  | "high"
  | "xhigh";

export class ModelNotConfiguredError extends Error {
  constructor(purpose: Purpose, detail: string) {
    super(`No model configured for purpose "${purpose}": ${detail}`);
    this.name = "ModelNotConfiguredError";
  }
}

/** The `provider:model` string in force for a purpose. */
export function modelRef(purpose: Purpose): string {
  const ref = PURPOSE_ENV[purpose] ?? env.LLM_DEFAULT;
  if (!ref) {
    throw new ModelNotConfiguredError(
      purpose,
      `set LLM_${purpose.toUpperCase()} or LLM_DEFAULT in .env to "provider:model" ` +
        `(see .env.example for the price sheet). There is no default vendor.`,
    );
  }

  const provider = ref.split(":")[0] as ProviderId;
  if (!hasCredential(provider)) {
    throw new ModelNotConfiguredError(
      purpose,
      `it resolves to "${ref}" but ${PROVIDER_KEY_ENV[provider] ?? provider} is not set. ` +
        `Falling back to another vendor would spend money somewhere you did not choose.`,
    );
  }

  return ref;
}

export function providerOf(purpose: Purpose): ProviderId {
  return modelRef(purpose).split(":")[0] as ProviderId;
}

/**
 * The model instance for a purpose, wrapped so every call it makes is
 * ledgered.
 *
 * The wrapping happens here rather than at the call sites because the agent
 * loop calls the model from inside the SDK: anything that merely wraps our own
 * helper functions is bypassed the moment an agent is involved.
 */
export function model(purpose: Purpose): LanguageModel {
  const ref = modelRef(purpose);
  return wrapLanguageModel({
    model: registry.languageModel(
      ref as Parameters<typeof registry.languageModel>[0],
    ),
    middleware: ledgerMiddleware(purpose, ref),
  });
}

/**
 * Reasoning effort is worth tuning without a deploy, because it dominates the
 * bill on the bulk passes. Measured on DeepSeek Flash: 92% of the output tokens
 * a scoring call produces are reasoning tokens — about 2,085 of 2,255 — for a
 * task whose answer is a number and three short phrases. Turning that down is
 * the single largest cost lever in the product, so it is an env setting:
 * LLM_REASONING_SCORE=none, and so on per purpose.
 */
export function reasoningFor(purpose: Purpose): ReasoningEffort {
  const override = process.env[`LLM_REASONING_${purpose.toUpperCase()}`];
  if (override && isReasoningEffort(override)) return override;
  return REASONING_DEFAULTS[purpose];
}

const EFFORTS: ReasoningEffort[] = [
  "none",
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh",
];

function isReasoningEffort(value: string): value is ReasoningEffort {
  return (EFFORTS as string[]).includes(value);
}

/**
 * Vendor quirks that cannot be expressed as a portable setting.
 *
 * Kept as a lookup rather than scattered `if (provider === ...)` checks at call
 * sites — when a vendor changes its mind, this is the only place to edit.
 */
export function quirksFor(ref: string): {
  /** Some reasoning models reject an explicit temperature outright. */
  supportsTemperature: boolean;
} {
  const [provider, modelId] = ref.split(":");

  // Anthropic's reasoning-capable models reject `temperature` alongside
  // extended thinking rather than ignoring it.
  const anthropicReasoner =
    provider === "anthropic" && /sonnet-5|opus-5|haiku-4-5/.test(modelId ?? "");

  return { supportsTemperature: !anthropicReasoner };
}

/** Which purposes are actually usable right now — for the smoke script and /settings. */
export function configuredPurposes(): Array<{
  purpose: Purpose;
  ref: string | null;
  error: string | null;
}> {
  return PURPOSES.map((purpose) => {
    try {
      return { purpose, ref: modelRef(purpose), error: null };
    } catch (error) {
      return {
        purpose,
        ref: null,
        error: error instanceof Error ? error.message : String(error),
      };
    }
  });
}
