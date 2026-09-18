import { createEnv } from "@t3-oss/env-nextjs";
import { z } from "zod";

/**
 * The single source of truth for configuration.
 *
 * Both entry points import this — the Next app and the worker — and nothing
 * else reads `process.env` directly.
 */

/**
 * `provider:model`, e.g. `openai:gpt-5.6-luna`. Never a bare model name: the
 * provider half is what keeps vendor choice out of the code.
 */
const modelRef = z
  .string()
  .regex(
    /^[a-z0-9-]+:[A-Za-z0-9._/@-]+$/,
    'expected "provider:model", e.g. openai:gpt-5.6-luna',
  )
  .optional();

const rpm = z.coerce.number().int().positive().optional();

export const env = createEnv({
  server: {
    NODE_ENV: z
      .enum(["development", "test", "production"])
      .default("development"),

    DATABASE_URL: z.url(),

    BETTER_AUTH_SECRET: z.string().min(32).optional(),
    BETTER_AUTH_URL: z.url().default("http://localhost:3000"),
    GOOGLE_CLIENT_ID: z.string().optional(),
    GOOGLE_CLIENT_SECRET: z.string().optional(),

    // Provider credentials — any subset. An unset provider is simply not
    // available; `lib/llm/models.ts` refuses a purpose pointed at it rather
    // than quietly falling back to a different vendor.
    OPENAI_API_KEY: z.string().optional(),
    GOOGLE_GENERATIVE_AI_API_KEY: z.string().optional(),
    DEEPSEEK_API_KEY: z.string().optional(),
    ANTHROPIC_API_KEY: z.string().optional(),
    LLM_COMPAT_BASE_URL: z.url().optional(),
    LLM_COMPAT_API_KEY: z.string().optional(),

    // Model per purpose. LLM_DEFAULT is required *to use the LLM layer*, not to
    // boot: the app must come up before any provider key exists so the database,
    // auth and worker steps are verifiable on their own. `models.ts` throws with
    // a pointer to .env.example when a purpose resolves to nothing.
    LLM_DEFAULT: modelRef,
    LLM_CHAT: modelRef,
    LLM_EXTRACT: modelRef,
    LLM_SCORE: modelRef,
    LLM_EVALUATE: modelRef,
    LLM_TAILOR: modelRef,
    LLM_VERIFY: modelRef,
    LLM_MERGE: modelRef,

    LLM_RPM_OPENAI: rpm,
    LLM_RPM_GOOGLE: rpm,
    LLM_RPM_DEEPSEEK: rpm,
    LLM_RPM_ANTHROPIC: rpm,
    LLM_RPM_COMPAT: rpm,

    RAPIDAPI_KEY: z.string().optional(),
    JSEARCH_ENABLED: z
      .enum(["true", "false"])
      .default("false")
      .transform((v) => v === "true"),
    SEARCH_API_KEY: z.string().optional(),

    STORAGE_DIR: z.string().default("./data"),
    WORKER_CONCURRENCY: z.coerce.number().int().positive().default(4),
  },

  client: {},

  /**
   * Server variables are read straight from process.env. Client variables would
   * have to be listed here so Next can inline them at build time; there are
   * none — everything intelligent runs server-side.
   */
  experimental__runtimeEnv: {},

  /** `FOO=` in .env means unset, so an empty placeholder never satisfies a key. */
  emptyStringAsUndefined: true,

  /** `docker build` has no .env; the running container validates for real. */
  skipValidation: process.env.SKIP_ENV_VALIDATION === "1",
});
