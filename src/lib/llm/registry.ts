import { anthropic } from "@ai-sdk/anthropic";
import { deepSeek } from "@ai-sdk/deepseek";
import { google } from "@ai-sdk/google";
import { openai } from "@ai-sdk/openai";
import { createOpenAICompatible } from "@ai-sdk/openai-compatible";
import { createProviderRegistry } from "ai";

import { env } from "@/env";

/**
 * Every provider Foray can talk to, behind one `provider:model` lookup.
 *
 * There is deliberately no default vendor here. Which model serves which
 * purpose is an env setting (see models.ts), so switching the scoring model
 * from DeepSeek to OpenAI is an .env edit, not a code change.
 */

const vendors = {
  openai,
  google,
  anthropic,
  // The package exports `deepSeek`; the registry key is what appears in env.
  deepseek: deepSeek,
};

/**
 * The OpenAI-compatible slot — OpenRouter, Together, a local server. It is only
 * registered when a base URL exists, since the factory requires one.
 */
const providers = env.LLM_COMPAT_BASE_URL
  ? {
      ...vendors,
      compat: createOpenAICompatible({
        name: "compat",
        baseURL: env.LLM_COMPAT_BASE_URL,
        apiKey: env.LLM_COMPAT_API_KEY,
      }),
    }
  : vendors;

export const registry = createProviderRegistry(providers);

export type ProviderId =
  | "openai"
  | "google"
  | "anthropic"
  | "deepseek"
  | "compat";

/**
 * Which env var carries each provider's credential. Used to fail with a
 * useful message rather than a 401 from the vendor.
 */
export const PROVIDER_KEY_ENV: Record<ProviderId, string> = {
  openai: "OPENAI_API_KEY",
  google: "GOOGLE_GENERATIVE_AI_API_KEY",
  anthropic: "ANTHROPIC_API_KEY",
  deepseek: "DEEPSEEK_API_KEY",
  compat: "LLM_COMPAT_API_KEY",
};

export function hasCredential(provider: ProviderId): boolean {
  switch (provider) {
    case "openai":
      return Boolean(env.OPENAI_API_KEY);
    case "google":
      return Boolean(env.GOOGLE_GENERATIVE_AI_API_KEY);
    case "anthropic":
      return Boolean(env.ANTHROPIC_API_KEY);
    case "deepseek":
      return Boolean(env.DEEPSEEK_API_KEY);
    case "compat":
      return Boolean(env.LLM_COMPAT_BASE_URL);
  }
}

export function configuredProviders(): ProviderId[] {
  return (
    ["openai", "google", "anthropic", "deepseek", "compat"] as ProviderId[]
  ).filter(hasCredential);
}
