# Area map: LLM layer and LLM pipelines (outside the chat agent)

Repo root: `/Users/haydenbarolette/Projects/JobSearchAgent/.claude/worktrees/middleware-django-migration-389b1a`.
All paths below are relative to it. Line numbers are from the current HEAD (`f82e21c`).

Scope:

- `src/lib/llm/**`: registry, models (purpose to model, vendor quirks, reasoning), context (async-local attribution), limiter (token bucket), pricing, ledger middleware, `callLLM`, `generateStructured`.
- `src/lib/ranking/**`: search brief, prefilter (the LLM call `deriveSearch` plus SQL), fast score, deep eval, schemas, bake-off fixture.
- `src/lib/resume/**`: schema, prose rendering for models, extraction (text and vision), PDF/DOCX parsing and garble detection, provenance stamping, tailoring, verification, HTML template.
- `src/lib/profile/**`: interview slots, onboarding state, settings. None of these call a model; they shape prompts and gates.
- Cross-references: LLM call sites in other areas (`sources/careers-page.ts`, `agents/tools/companies.ts`), orchestration in `runs/*.ts`, `api/uploads/route.ts`, and the scripts `scripts/llm-smoke.ts` and `scripts/model-bakeoff.ts`.

Installed versions (package.json:30-69 and node_modules): `ai@7.0.106`, `@ai-sdk/anthropic@4.0.57`, `@ai-sdk/openai@4.0.70`, `@ai-sdk/google@4.0.75`, `@ai-sdk/deepseek@3.0.48`, `@ai-sdk/openai-compatible@3.0.52`, `@ai-sdk/provider-utils@5.0.44`, `zod@4.6.5`, `unpdf@1.8.1` (+ `@napi-rs/canvas`), `mammoth@1.12.3`.

---

## 1. Configuration (env)

`src/env.ts` is the only reader of `process.env`, with one exception: `reasoningFor()` (section 3).

| Var | Schema | Where | Meaning |
|---|---|---|---|
| `OPENAI_API_KEY`, `GOOGLE_GENERATIVE_AI_API_KEY`, `DEEPSEEK_API_KEY`, `ANTHROPIC_API_KEY` | optional string (env.ts:41-44) | registry.ts:62-75 | Provider credentials. Any subset may be set. An unset provider is unavailable. |
| `LLM_COMPAT_BASE_URL` | optional url (env.ts:45) | registry.ts:30-39 | Registers the `compat` OpenAI-compatible provider only when set. |
| `LLM_COMPAT_API_KEY` | optional (env.ts:46) | registry.ts:36 | Key for compat. `hasCredential("compat")` checks the BASE URL, not this key (registry.ts:72-73). |
| `LLM_DEFAULT`, `LLM_CHAT`, `LLM_EXTRACT`, `LLM_SCORE`, `LLM_EVALUATE`, `LLM_TAILOR`, `LLM_VERIFY`, `LLM_MERGE` | optional `modelRef`, regex `^[a-z0-9-]+:[A-Za-z0-9._/@-]+$` (env.ts:15-21, 52-59) | models.ts:46-54, 91 | `provider:model` per purpose. A purpose falls back to `LLM_DEFAULT`. They are not required at boot; failure happens at use. |
| `LLM_REASONING_<PURPOSE>` (CHAT, EXTRACT, SCORE, EVALUATE, TAILOR, VERIFY, MERGE) | not in env schema; read from `process.env` at call time (models.ts:143) | models.ts:142-146 | Override of reasoning effort: `none|minimal|low|medium|high|xhigh`. An invalid value is ignored silently. `.env.example:66` ships `LLM_REASONING_SCORE=none`. |
| `LLM_RPM_OPENAI`, `LLM_RPM_GOOGLE`, `LLM_RPM_DEEPSEEK`, `LLM_RPM_ANTHROPIC`, `LLM_RPM_COMPAT` | `z.coerce.number().int().positive().optional()` (env.ts:23, 61-65) | limiter.ts:64-70 | Requests per minute per provider. Default 300 (limiter.ts:28). `.env.example:78` sets `LLM_RPM_OPENAI=500`. |
| `SKIP_ENV_VALIDATION=1` | | env.ts:98 | Skips validation (docker build). |
| `emptyStringAsUndefined: true` | | env.ts:95 | `FOO=` counts as unset. |

`.env.example:32-50` documents the price sheet and that `extract` and `merge` need a vision-capable model.

---

## 2. Registry: `src/lib/llm/registry.ts`

- Vendors (registry.ts:18-24): `openai`, `google`, `anthropic`, and `deepseek`. The deepseek package export is `deepSeek`, but the registry key is `deepseek`.
- `compat` (registry.ts:30-39) is `createOpenAICompatible({ name: "compat", baseURL: LLM_COMPAT_BASE_URL, apiKey: LLM_COMPAT_API_KEY })`, registered only when the base URL is set.
- `registry = createProviderRegistry(providers)` (registry.ts:41). The default separator is `:`, so `registry.languageModel("openai:gpt-5.6-luna")` resolves to `openai("gpt-5.6-luna")`.
- `ProviderId` is `openai|google|anthropic|deepseek|compat` (registry.ts:43-48).
- `PROVIDER_KEY_ENV` maps each provider to the name of its env var, used for error messages (registry.ts:54-60). For compat the message names `LLM_COMPAT_API_KEY` even though the check is on the base URL.
- `hasCredential(provider)` (registry.ts:62-75) is a boolean on the env var. For an unknown provider string the switch falls through and returns `undefined`, which is falsy.
- `configuredProviders()` returns the providers whose credential is set, in the fixed order `openai, google, anthropic, deepseek, compat` (registry.ts:77-81). Consumers: settings page (settings/page.tsx:65) and llm-smoke (llm-smoke.ts:25).
- AI SDK default behaviour this relies on: `openai(modelId)` creates a **Responses API** model in @ai-sdk/openai 4.x (node_modules/@ai-sdk/openai/dist/index.js:12059). Google uses the Gemini API key (generativelanguage), not Vertex.

---

## 3. Purposes, model resolution, vendor quirks: `src/lib/llm/models.ts`

- `Purpose` is `chat|extract|score|evaluate|tailor|verify|merge` (models.ts:21-38). The Postgres enum `llm_purpose` has the same values (schema/enums.ts:123-131).
- `VISION_PURPOSES` is `{extract, merge}` (models.ts:41-44). It is exported but **unused** anywhere else.
- `merge` has **no call site** in the codebase. It is reserved for the LinkedIn merge ("Step 9").
- `PURPOSE_ENV` maps a purpose to its env var (models.ts:46-54).
- `REASONING_DEFAULTS` (models.ts:64-72): chat=`low`, extract=`low`, score=`minimal`, evaluate=`medium`, tailor=`high`, verify=`low`, merge=`low`. The reason (models.ts:57-63): tailoring gets the most because a fabricated bullet is the worst failure; bulk passes get the least.
- `ReasoningEffort` is `none|minimal|low|medium|high|xhigh` (models.ts:74-80).
- `ModelNotConfiguredError(purpose, detail)`: message `No model configured for purpose "<p>": <detail>`, name `ModelNotConfiguredError` (models.ts:82-87).
- `modelRef(purpose): string` (models.ts:90-110):
  1. `ref = PURPOSE_ENV[purpose] ?? env.LLM_DEFAULT`. If unset, it throws with detail `set LLM_<PURPOSE> or LLM_DEFAULT in .env to "provider:model" (see .env.example for the price sheet). There is no default vendor.`
  2. `provider = ref.split(":")[0]`. If `!hasCredential(provider)`, it throws with detail `it resolves to "<ref>" but <ENVVAR or provider> is not set. Falling back to another vendor would spend money somewhere you did not choose.`
  3. **Invariant:** it never falls back to another vendor.
- `providerOf(purpose)` returns the provider half of `modelRef` (models.ts:112-114).
- `model(purpose): LanguageModel` (models.ts:124-132) is `wrapLanguageModel({ model: registry.languageModel(ref), middleware: ledgerMiddleware(purpose, ref) })`. Every call creates a fresh wrapped instance. The ledger sits **on the model**, so agent loops are ledgered too (models.ts:116-123).
- `reasoningFor(purpose)` (models.ts:134-146) reads `process.env["LLM_REASONING_" + PURPOSE]` at call time, validates it against `EFFORTS` (models.ts:148-159), and otherwise returns the default. The comment records that on DeepSeek Flash about 92% of scoring output tokens are reasoning (2,085 of 2,255). `.env.example:65` says turning reasoning off took scoring from about $1.50 to $0.24 per 500 postings.
- `quirksFor(ref)` (models.ts:167-179) is **the only per-vendor quirk in code**:
  - `supportsTemperature = !(provider === "anthropic" && /sonnet-5|opus-5|haiku-4-5/.test(modelId))`. The reason (models.ts:173-174): Anthropic's reasoning-capable models reject `temperature` alongside extended thinking rather than ignoring it.
  - No call site in this area passes `temperature`, so today the quirk has no effect (grep finds no `temperature`, `maxOutputTokens`, `providerOptions` or cache-control anywhere in `src/` or `scripts/`).
- `configuredPurposes()` returns, for each purpose in `PURPOSES` order, `{purpose, ref, error}`, with `error` set to the thrown message (models.ts:182-198). Consumers: settings page (purpose table: model, provider label, `reasoningFor`, `rateFor` price) and llm-smoke.

### 3a. What the AI SDK does with `reasoning`, per provider (behaviour relied on)

`callLLM` always passes the top-level `reasoning: <effort>`. The SDK maps it as follows (`provider-utils` `isCustomReasoning(r)` is true for anything except `undefined` and `"provider-default"`):

- **OpenAI** (Responses): `reasoningEffort = providerOptions.openai.reasoningEffort ?? reasoning`, passed through verbatim. If the model's capability table does not list that effort, it is dropped with a warning (openai dist/index.js:1146-1156). OpenAI reasoning models also warn and drop temperature and topP.
- **Anthropic** (anthropic dist/index.js:6074-6113, 4125-4140):
  - `none` gives `thinking: {type: "disabled"}`.
  - On adaptive-thinking models, `thinking: {type: "adaptive", display: "summarized"}` plus `effort`, mapped `minimal→low`, `low→low`, `medium→medium`, `high→high`, and `xhigh→xhigh` (or `max` if xhigh is unsupported).
  - On other models, `thinking: {type: "enabled", budgetTokens}` with `budgetTokens = min(maxOut, max(1024, round(maxOut * pct)))`, where pct is minimal 0.02, low 0.1, medium 0.3, high 0.6, xhigh 0.9 (provider-utils index.js:1777-1805).
- **Google**:
  - Gemini 3 models: `thinkingLevel`. `none` maps to the model's minimum level; the effort map is `minimal→min`, `low`, `medium`, `high`, and `xhigh→high`. The minimum level is `low` for `gemini-flash-latest` and for gemini-X.Y-flash with version 3.7 or higher, otherwise `minimal` (google dist/index.js:2555-2600).
  - Gemini 2.5: `thinkingBudget` (0 for `none`).
- **DeepSeek** (deepseek dist/index.js:840-868): `thinking: {type: reasoning === "none" ? "disabled" : "enabled"}`. When not `none`, `reasoning_effort` is mapped `minimal→low`, `low→low`, `medium→high`, `high→high`, `xhigh→max`. Temperature and topP have no effect while thinking is on.
- **compat**: `reasoning_effort = reasoning` verbatim (openai-compatible dist/index.js:602).

### 3b. Structured output per provider (`Output.object({schema})`)

- **OpenAI**: native `json_schema` with `strict: true` by default (`strictJsonSchema ?? true`, openai dist:1168-1192). This is why every schema field is required, with absence written as `.nullable()` (schemas.ts:6-8, resume/schema.ts:101-114, tailor.ts:65).
- **Anthropic**: native `output_format` json_schema when the model supports structured output; otherwise a forced tool named `json` ("Respond with a JSON object.") (anthropic dist:4074-4082, 4193-4196).
- **DeepSeek** and **compat**: `supportsStructuredOutputs` defaults to false, so the SDK **injects a system message** `Return JSON that conforms to the following schema: <JSON.stringify(schema)>` and sets `response_format: {type: "json_object"}` (deepseek dist:148-163, 882). This is the path where models sometimes answer with reasoning and no object, which motivates the NoOutputGeneratedError retry (structured.ts:42-46).
- Only `.describe()` strings travel to the model in the JSON Schema; JSDoc comments on zod fields do not. Fields with `.describe()`: `DeepEval.seniority_fit` and `DeepEval.salary_fit` (schemas.ts:90-100), the companies tool's `why`, and the smoke `Answer.city`.

### 3c. Transport retry and timeouts (SDK defaults relied on)

- `generateText` wraps each `doGenerate` in `retryWithExponentialBackoffRespectingRetryHeaders` with `maxRetries=2`, `initialDelayInMs=2000`, and `backoffFactor=2` (ai dist:2850-2861). A retry happens only on `APICallError.isRetryable`, which covers status 408, 409, 429 and 5xx (ai dist:435-437). The delay honours `retry-after-ms` and `retry-after` (seconds or HTTP-date) when the value is between 0 and 60s or below the backoff delay (ai dist:2822-2849).
- Because the retry wraps `doGenerate`, and the ledger middleware wraps `doGenerate`, **each transport attempt writes its own ledger row** (failed attempts get zero tokens plus an error message).
- The limiter (`acquire`) is taken **once per `callLLM`**, not once per transport retry.
- `timeout` (a number, in ms) is the total budget across steps and retries (ai dist:2262-2270). Only deep-eval sets it: 4 min (deep-eval.ts:45, 296).

---

## 4. Attribution context: `src/lib/llm/context.ts`

- `LlmContext = { userId?: string|null; runId?: string|null }` (context.ts:12-15).
- `withLlmContext(context, fn)` is `AsyncLocalStorage.run(context, fn)` (context.ts:20-22).
- `currentLlmContext()` returns the store or `{}` (context.ts:24-26).
- Why it exists (context.ts:3-10): agent loops call the model several frames below our code, so attribution cannot be threaded through parameters.
- Setters:
  - `callLLM` wraps every direct call (call.ts:50).
  - `api/chat/route.ts:66` uses `{userId: user.id}`.
  - `chat/follow-up.ts:136` uses `{userId}`.
- Port note: in Python this maps to `contextvars.ContextVar`. Thread pools do NOT propagate context automatically; use `contextvars.copy_context().run` or set it inside each worker.

---

## 5. Rate limiter: `src/lib/llm/limiter.ts`

- In-process token bucket **per provider id**. It is not shared across processes; the Next server and the worker each have their own.
- `DEFAULT_RPM = 300` (limiter.ts:28). The comment explains why 60 was too low: measured, a batch of 80 calls took 9s for the first 60 and 12.9s for the last 20.
- `TokenBucket(ratePerMinute)` (limiter.ts:30-62):
  - It starts **full** (`tokens = ratePerMinute`).
  - `refill()` adds `elapsedMinutes * rate`, capped at `rate`.
  - `take()` loops: refill; if `tokens >= 1`, decrement and return; otherwise sleep `max(ceil(((1 - tokens) / rate) * 60000), 25)` ms.
- `RPM` per-provider overrides come from env (limiter.ts:64-70). Bucket keys are the provider strings.
- `acquire(provider)` (limiter.ts:86-95) chains on a per-provider promise `tails` map, so waiters are FIFO-serialized per provider. A rejected waiter does not break the chain (`next.catch(() => undefined)`).
- **Who calls it:** only `callLLM` (call.ts:48). The chat agent (`ToolLoopAgent` with `model("chat")`) does **not** go through the limiter, despite the comment at limiter.ts:85 ("Every LLM call goes through here").

---

## 6. `callLLM`: `src/lib/llm/call.ts`

Signature (call.ts:39-44):

```
callLLM(purpose, args /* generateText args minus model, reasoning */, context = {}, overrides = {})
```

`CallOverrides = { model?, reasoning?, providerId? }`, used only by the bake-off and tests (call.ts:32-37).

Steps:

1. `ref = modelRef(purpose)`. This **throws even when `overrides.model` is given** if the purpose is not configured. Tests and the bake-off depend on `LLM_DEFAULT` being set.
2. `quirks = quirksFor(ref)`. Quirks come from the configured ref, **not** from an override model.
3. `await acquire(overrides.providerId ?? providerOf(purpose))`.
4. `withLlmContext(context, () => generateText({...args, model: overrides.model ?? model(purpose), reasoning: overrides.reasoning ?? reasoningFor(purpose), ...(supportsTemperature ? {} : {temperature: undefined})}))`.

It returns the full `generateText` result (`output`, `usage`, and so on). Callers pass `instructions` (AI SDK v7's name for the system prompt) plus either `prompt` or `messages`.

---

## 7. `generateStructured`: `src/lib/llm/structured.ts`

Signature (structured.ts:63-69):

```
generateStructured<T>(purpose, schema: ZodType<T>, args /* minus output, model */, context = {}, overrides = {})
  -> { value: T, usage: LanguageModelUsage, retried: boolean }
```

- `attempt()` calls `callLLM(purpose, {...args, output: Output.object({schema})}, context, overrides)` and reads `result.output` **inside the try**, because the getter throws when the output did not parse (structured.ts:70-80).
- **One retry**, only when `isRetryableOutputFailure(error)` (structured.ts:50-61):
  - `NoOutputGeneratedError` (no object at all; seen on DeepSeek) is retryable.
  - `NoObjectGeneratedError` with `finishReason === "length"` is NOT retried, since truncation would repeat.
  - `NoObjectGeneratedError` whose `cause` is a `TypeValidationError` (schema mismatch) or `instanceof SyntaxError` is retryable.
  - **Latent bug:** for unparseable JSON the SDK's cause is `JSONParseError`, which wraps the SyntaxError (provider-utils index.js:3299-3304; ai dist:4057-4066), so `cause instanceof SyntaxError` is false and malformed JSON is **not** retried. Decide in the port whether to keep this exact behaviour or the evident intent (retry on parse failure).
  - Anything else (transport errors after the SDK's own retries, ModelNotConfiguredError, timeouts) is rethrown.
- On retry it logs `console.warn("[llm] <purpose>: unusable structured output (<error.name>), retrying once")` and returns `{...second attempt, retried: true}`. A second failure propagates.
- Reason (structured.ts:13-21, 35-49): one retry is worth it; a second failure usually means the prompt or schema is wrong, and looping would multiply the bill.
- The SDK validates against zod after parsing (`safeValidateTypes`). Range constraints such as `.int().min(0).max(100)` and `DateString.min(1).max(40)` are therefore enforced locally and produce a TypeValidationError, which triggers the retry.

---

## 8. Ledger middleware and the `llm_calls` table

### 8a. Table `llm_calls` (schema/chat.ts:74-108)

| column | type | notes |
|---|---|---|
| `id` | uuid pk default random | |
| `user_id` | text FK user.id ON DELETE SET NULL | nullable |
| `run_id` | uuid FK runs.id ON DELETE SET NULL | nullable |
| `purpose` | enum `llm_purpose` NOT NULL | |
| `provider` | text NOT NULL | |
| `model` | text NOT NULL | |
| `input_tokens` | int NOT NULL default 0 | **uncached input plus cache-write tokens** (see below) |
| `output_tokens` | int NOT NULL default 0 | total output, reasoning included |
| `cached_input_tokens` | int NOT NULL default 0 | cache reads |
| `reasoning_tokens` | int NOT NULL default 0 | informational (already inside output) |
| `cost_usd` | numeric(12,6) NOT NULL default '0' | computed at call time; rows are never re-priced |
| `latency_ms` | int nullable | |
| `error` | text nullable | set when the call failed |
| `created_at` | timestamptz NOT NULL default now() | |

Indexes: `(user_id, created_at desc)`, `(run_id)`, `(purpose, model)` (schema/chat.ts:103-107).

DESIGN.md invariant 10 (DESIGN.md:59-64): every LLM call writes a row, enforced by middleware on the model rather than by discipline at call sites.

### 8b. `tokensFromProviderUsage(usage)` (ledger.ts:35-52)

The input is the provider-level usage shape (`LanguageModelV4Usage`):

```
{ inputTokens: { total, noCache, cacheRead, cacheWrite }, outputTokens: { total, text, reasoning } }
```

This is different from the `ai`-level shape, which uses `inputTokens` plus `inputTokenDetails.{noCacheTokens, cacheReadTokens, cacheWriteTokens}`.

- `cacheRead = inputTokens.cacheRead ?? 0` and `cacheWrite = inputTokens.cacheWrite ?? 0`.
- `noCache = inputTokens.noCache ?? max((inputTokens.total ?? 0) - cacheRead, 0)`. A provider that reports only a total still gets the right uncached share, and the value is never negative.
- Result: `{ inputTokens: noCache, outputTokens: outputTokens.total ?? 0, cachedInputTokens: cacheRead, cacheWriteTokens: cacheWrite, reasoningTokens: outputTokens.reasoning ?? 0 }`.
- A missing usage object gives all zeros.

Pinned by tests (src/test/unit/token-accounting.test.ts:25-90):

- total=1000, noCache=400, cacheRead=600 gives input 400 and cached 600.
- total=1000, cacheRead=250 gives input 750.
- total=100, cacheRead=400 gives input 0.
- Haiku with cacheWrite=1e6 and cacheRead=1000 costs 1.25 + 0.0001.
- On terra, 900 output tokens of which 500 are reasoning costs 900 × $12/M only.

### 8c. Row write: `write()` (ledger.ts:54-86)

- `{userId, runId}` comes from `currentLlmContext()`.
- `[provider, ...rest] = ref.split(":")` and `model = rest.join(":")`.
- `input_tokens = tokens.inputTokens + (tokens.cacheWriteTokens ?? 0)`. The reason (ledger.ts:71-73): tokens written into a provider cache were still read in full, and some providers report most of a prompt that way. Cost prices the two separately.
- `cached_input_tokens = cachedInputTokens` and `reasoning_tokens = reasoningTokens`.
- `cost_usd = costUsd(ref, tokens).toFixed(6)`, as a string.
- `latency_ms`, plus `error` or null.
- An insert failure is caught and logged as `"[llm] failed to write ledger row:"`, **never thrown**. Losing a ledger row must not fail work that already spent the money.
- Possible double count: when a provider reports only `total` plus `cacheWrite` (no `noCache`), `noCache` already includes the cache-write tokens, and `input_tokens` adds them again. Both the column and the cost are inflated in that case. Anthropic reports `noCache`, so this does not happen today.

### 8d. `ledgerMiddleware(purpose, ref)` (ledger.ts:89-171)

- `wrapGenerate`:
  - It times `doGenerate()`. On success it writes the row with the tokens from `result.usage` and returns the result.
  - On a throw it writes a row with zero tokens and `error = error.message`, then rethrows.
  - The row write is **awaited** before returning, so it adds DB latency to every call.
- `wrapStream`:
  - If `doStream()` throws, it writes an error row and rethrows.
  - Otherwise it pipes the stream through a TransformStream that captures `chunk.usage` from the `finish` part and writes the row once in `flush()` (`recordOnce`).
  - Gaps:
    - `flush` does not run on consumer cancel or abort, so an aborted stream may write no row, despite the comment at ledger.ts:161-162.
    - An `error` part in the stream is not recorded as an error.
    - Latency is measured to stream end.
  - Only the chat agent streams; every call in this area uses `generateText`.

### 8e. Read side: `src/lib/db/queries/spend.ts`

Every function takes `userId` first.

- `monthStart(at)`: UTC first-of-month (spend.ts:10-12).
- `spendSince(userId, since)`: `sum(cost_usd)::float8`, `count(*)::int`, `sum(input_tokens + cached_input_tokens)::float8`, `sum(output_tokens)::float8`, where `user_id = $1 and created_at >= since` (spend.ts:14-25).
- `spendByPurpose`: grouped by purpose, ordered by `sum(cost) desc` (spend.ts:27-38).
- `spendByModel`: grouped by provider and model, with `failures = count(*) filter (where error is not null)` and `avgLatencyMs` (spend.ts:40-54).
- `spendByDay`: `to_char(date_trunc('day', created_at at time zone 'UTC'), 'YYYY-MM-DD')` grouped and ordered by column 1 (spend.ts:57-67).
- The activity page also sums `cost_usd` per `run_id` (activity/page.tsx:74-90).

---

## 9. Pricing: `src/lib/llm/pricing.ts`

Prices are USD per million tokens, checked 2026-09-17 (pricing.ts:1-9).

- `Rate = {input, output, cachedInput?, cacheWrite?, reasoning?}`:
  - An absent `cachedInput` bills cached input at the `input` rate.
  - An absent `cacheWrite` bills cache writes at the `input` rate.
  - `reasoning` defaults to 0, because every provider counts reasoning inside output.
- `TimeBandedRate = {bands: [{fromHourUtc (inclusive), toHourUtc (exclusive; wraps past midnight if smaller), rate}], fallback}` (pricing.ts:41-50).

`PRICING` (pricing.ts:64-102):

| key | input | output | cachedInput | cacheWrite |
|---|---|---|---|---|
| `deepseek:deepseek-flash` | band 17:00-00:00 UTC: 0.15 / 0.6 / 0.015; fallback: 0.3 / 1.2 / 0.03 | | | |
| `openai:gpt-5.6-luna` | 0.2 | 1.2 | 0.02 | |
| `google:gemini-3.8-flash` | 0.75 | 3.75 | 0.19 | |
| `anthropic:claude-haiku-4-5` | 1 | 5 | 0.1 | 1.25 |
| `openai:gpt-5.6-terra` | 2 | 12 | 0.2 | |
| `google:gemini-3.1-pro-preview` | 2 | 12 | 0.5 | |
| `anthropic:claude-sonnet-5` | 2 | 10 | 0.2 | 2.5 |
| `anthropic:claude-opus-5` | 5 | 25 | 0.5 | 6.25 |

DeepSeek's real off-peak window is 16:30-00:30 UTC. The table approximates it on hour boundaries, erring toward over-reporting cost (pricing.ts:67-68).

Functions:

- `hourInBand(hour, from, to)`: when `from <= to`, `from <= hour < to`; otherwise `hour >= from || hour < to` (pricing.ts:104-106). Note that 17 to 0 is the wrap case (17-23).
- `rateFor(ref, at = now)` (pricing.ts:109-119) returns null when unpriced, the flat rate as-is, or the band matching `at.getUTCHours()` and otherwise `fallback`.
- `costUsd(ref, tokens, at = now)` (pricing.ts:135-156) returns 0 when unpriced and never throws. Otherwise:

  ```
  in/1e6*input + cached/1e6*(cachedInput ?? input) + cacheWrite/1e6*(cacheWrite ?? input)
  + out/1e6*output + reasoning/1e6*(reasoning ?? 0)
  ```

  The comment at pricing.ts:60-62 says an unpriced model "logs a warning". **No warning is logged in code.**
- `isPriced(ref)` is `ref in PRICING` (pricing.ts:159-161).

Tests (src/test/unit/pricing.test.ts) pin: banding by UTC hour, the wrap past midnight, flat rates, cached versus uncached, cache writes, no reasoning double count, unpriced cost of 0, and off-peak being cheaper.

---

## 10. Ranking: search brief, `src/lib/ranking/brief.ts`

No LLM call here, but `deriveSearch` produces the brief.

- `SearchBrief` (zod, brief.ts:23-33), each field nullable:
  - `roles: string[]|null`
  - `seniority: Seniority|null`, using the profile enum `intern|new_grad|entry|mid|senior|staff_plus`
  - `locations: string[]|null`
  - `remote: RemotePreference|null`, from `remote|hybrid|onsite|any`
  - `companies: string[]|null`
  - `focus: string|null`
- `EMPTY_BRIEF` has every field null (brief.ts:37-44).
- `SAVED_PREFERENCES_PROMPT = "Search with my saved preferences"` (brief.ts:47).
- `requestText(prompt)` returns the trimmed text, or null when it is empty or equals the placeholder (brief.ts:50-53).
- `parseBrief(value)`: `SearchBrief.partial().safeParse(value ?? {})`. On success it returns `cleanBrief`, otherwise `EMPTY_BRIEF` (brief.ts:56-59).
- `cleanBrief(partial)` (brief.ts:65-86):
  - Lists are trimmed, blanks dropped, duplicates removed case-insensitively (first spelling kept), and an empty list becomes null.
  - `seniority` and `remote` pass through as `?? null`.
  - `focus` is trimmed, or null.
- `mergeBrief(derived, hints)` cleans both sides, then takes `over.X ?? base.X` field by field, so the chat agent's hints win (brief.ts:93-107).
- `isEmptyBrief`: every value is null (brief.ts:109-111).
- `applyBrief(preferences, brief)` returns `{...preferences, target_roles: brief.roles ?? prefs.target_roles, seniority, locations, remote_pref: brief.remote ?? prefs.remote_pref}` (brief.ts:114-125). **`companies` and `focus` are not written into preferences.** Callers use `brief.companies` and `brief.focus` directly.
- `describeBrief(brief)` (brief.ts:151-168) returns null if `roles`, `seniority`, `locations`, `remote` and `companies` are all null (`focus` is ignored). Otherwise:
  - `what` is `"<joinOr(roles)> roles"`, or `"roles"`.
  - When any role matches `LEVEL_WORD` (`/^(senior|sr\.?|staff|principal|lead|junior|jr\.?|entry|new[- ]grad|intern|mid)\b/i`), the level is not prefixed.
  - Otherwise, when `seniority` is set, the text starts with `LEVEL[seniority]`: intern "Internship", new_grad "New-grad", entry "Entry-level", mid "Mid-level", senior "Senior", staff_plus "Staff-level and above".
  - Without a level prefix, the first character is capitalised.
  - Then ` at <joinOr(companies)>`, `, in <joinOr(locations)>`, and `, <REMOTE[remote]>`, where remote is "remote only", hybrid is "hybrid", onsite is "on site", and any adds nothing.
- `joinOr`: `a`, `a or b`, `a, b or c` (brief.ts:170-174).

---

## 11. Ranking: prefilter, `src/lib/ranking/prefilter.ts`

### 11a. LLM call #1: `deriveSearch` (purpose `score`, cheap tier)

- Constant `MAX_AGE_DAYS = 45` (prefilter.ts:35).
- Instructions: `SEARCH_INSTRUCTIONS` at prefilter.ts:60-98, a static string.
- Output schema `DerivedSearch` (prefilter.ts:100-105): `{ brief: SearchBrief, title_include: string[], title_exclude: string[], keywords: string[] }`.
- The internal `DerivedCriteria` type (prefilter.ts:37-56) is `{title_include, title_exclude, keywords, title_keep?}`. `title_keep` is set only by `guardSeniority`, never by the model, and is absent on criteria cached before it existed.
- Signature (prefilter.ts:118-124): `deriveSearch({preferences, request: string|null, hints?: Partial<SearchBrief>|null, context?}) -> {brief, criteria}`.

Prompt assembly (prefilter.ts:125-148): `hints = cleanBrief(hints ?? {})`, and `resolved` keeps only the non-null hint fields. The prompt is these parts, nulls filtered, joined with `\n\n`:

1. `Saved preferences:\n${JSON.stringify(preferences, null, 2)}`
2. Either `This search request:\n<request>` or `This search request: none. Search with the saved preferences; every brief field is null.`
3. When `resolved` is non-empty: `Already resolved for this search (treat these as stated in the request):\n${JSON.stringify(resolved, null, 2)}`

Reasoning is `reasoningFor("score")` (default `minimal`; `.env.example` sets `none`). Temperature is not set. There is one structured retry.

Post-processing (prefilter.ts:150-168):

- If there is a request, `read = {...value.brief, remote: value.brief.remote === "any" ? null : value.brief.remote}`. Models answer "any" for an unstated preference, which would override a saved "onsite"; "any" only counts when the chat agent set it via hints. With no request, `read = {}`: the model's brief is **discarded** entirely.
- `brief = mergeBrief(read, hints)`.
- `criteria = guardSeniority({title_include: dedupe(value.title_include.map(withoutLevel).map(escapeForRegex)), title_exclude: value.title_exclude.map(escapeForRegex).filter(Boolean), keywords: value.keywords.filter(Boolean)}, brief.seniority ?? preferences.seniority, brief.roles ?? preferences.target_roles, {companies: Boolean(brief.companies)})`.
- Caching is done by the caller: `runs.input.brief` and `runs.input.criteria` are saved (search-run.ts:164-174), so a resumed run does not pay twice.

### 11b. Deterministic helpers

- `wordPattern(phrase)` is `(^|[^a-z])` + phrase with spaces replaced by `[^a-z]+` + `([^a-z]|$)` (prefilter.ts:172-174). It is used case-insensitively in both Postgres (`~*`) and JS.
- `EARLY_CAREER_WORDS` (prefilter.ts:177-197): intern, internship, new grad, new graduate, graduate program, junior, jr, entry level, associate, early career, university grad, resident, residency, apprentice, apprenticeship, fellow, fellows, fellowship, rotational.
- `SENIOR_WORDS` (prefilter.ts:199-211): senior, sr, staff, principal, lead, manager, director, head of, vice president, vp, distinguished.
- `guardSeniority(criteria, seniority, roles?, {companies?})` (prefilter.ts:230-257) returns the criteria unchanged unless seniority is `intern`, `new_grad` or `entry`. Otherwise:
  - `early` is a regex OR of the early-career `wordPattern`s.
  - `wanted = roles.join(" ").toLowerCase()`.
  - `senior` = the `wordPattern`s of SENIOR_WORDS not already present in `wanted` ("associate product manager" keeps "manager").
  - `kept` = title_exclude terms that do not match `early` (after removing backslashes).
  - `title_include` gains every early-career wordPattern not already present, **only when `companies` is set**.
  - `title_exclude = [...kept, ...senior not already in kept]`.
  - `title_keep` = every early-career wordPattern.
  - Reason (prefilter.ts:213-229): literal "staff software engineer" missed "Staff+ Software Engineer", which let 45 staff roles in. The model also excluded "new grad" from a new-grad search.
- `withoutLevel(phrase)`: trims, then strips repeated leading `(senior|sr\.?|staff|principal|junior|jr\.?|mid[- ]level|entry[- ]level)\s+`, case-insensitively, and trims again (prefilter.ts:265-271). It deliberately does not strip "lead".
- `escapeForRegex(phrase)`: `trim().toLowerCase()`, then escapes `.*+?^${}()|[]\` (prefilter.ts:278-280).
- `buildTitlePatterns(criteria)` returns null when `title_include` is empty. Otherwise `{include: join("|"), exclude: join("|") or null, keep: join("|") or null}` (prefilter.ts:558-571).
- `titleMatches(title, criteria)` is the JS mirror of the SQL: it must match include; then a keep match lets it survive; otherwise it must not match exclude (prefilter.ts:577-588).
- `companyNamePattern(name)`: lowercased, split on `[^a-z0-9]+`, each word escaped, returned as `^w1[^a-z0-9]+w2...\y`, or null (prefilter.ts:441-450). `\y` is Postgres's word boundary.
- `BAY_AREA_PLACES` is a Postgres regex `\y(san francisco|sf|bay area|...|los gatos)\y` with 23 places (prefilter.ts:460-484).
- `roleKey(jobs)` is the SQL `lower(regexp_replace(company_name,'[^a-zA-Z0-9]+','','g')) || '|' || btrim(lower(regexp_replace(title,'[^a-zA-Z0-9+#]+',' ','g')))` (prefilter.ts:596-598).

### 11c. `prefilterJobs({userId, preferences, criteria, companies?, limit = 600})` (prefilter.ts:298-414)

The SQL is built from these conditions, ANDed together:

1. `jobs.is_active = true`.
2. A company filter, when given: `company_id IN ids` OR `company_name ~* companyNamePattern(name)` for each name. When there is no company filter, `(posted_at IS NULL OR posted_at >= now - 45d)`. The age cutoff is skipped for a named-company search.
3. `locationClause(preferences)` (prefilter.ts:486-531), ORed together:
   - When any location matches `/bay area|san francisco|sf|silicon valley|peninsula/i`, metro `bay-area`: `location_norm->>'metro' in ('bay-area')` OR `locations::text ~* BAY_AREA_PLACES`.
   - For each location, `city = split(",")[0].trim()`; when `city.length > 2` and it does not match `/bay area|remote|anywhere/i`, `locations::text ilike '%city%'`.
   - `remote = 'remote'` when remote_pref is `remote` or `any`.
   - `remote = 'hybrid'` when remote_pref is `hybrid`.
   - No clause at all when nothing applies.
4. `titleClause`: `title ~* include AND (title !~* exclude OR title ~* keep)`. Without keep it is `title !~* exclude`. There is no clause when include is empty.
5. `canonical_job_id IS NULL`.
6. Dismissed: `jobs.id NOT IN (job_matches where user and stage in ('rejected','archived'))`.
7. Known roles: `(roleKey NOT IN knownKeys) OR jobs.id IN knownIds`, where `known` comes from the user's job_matches joined to jobs.

Then:

- Dedupe: `SELECT DISTINCT ON (roleKey) id, posted_at ... ORDER BY roleKey, (source='jsearch')::int, posted_at desc nulls last`.
- Outer query: `ORDER BY posted_at desc nulls last LIMIT limit`.
- The funnel count: `count(*) filter (where is_active)` over all jobs.

Returns `{jobIds, funnel: {activeInMirror, selected, cappedAt: rows.length === limit ? limit : null}}`.

---

## 12. Ranking: fast score, `src/lib/ranking/fast-score.ts` (LLM call #2, purpose `score`)

Constants:

- `MAX_DESCRIPTION_CHARS = 4000` (fast-score.ts:28)
- `DEFAULT_SCORE_THRESHOLD = 55` (fast-score.ts:30); the settings default is also 55
- `CONCURRENCY = 16` (fast-score.ts:39). Measured: 16 postings took 6.1s at 8-way and 3.0s at 16-way.
- `BATCH = 40` (fast-score.ts:151)

Prompts:

- `RUBRIC` at fast-score.ts:41-61, static, with bands 85-100, 70-84, 55-69, 30-54 and 0-29, plus rules.
- `candidateBlock({preferences, focus, resume})` (fast-score.ts:64-102) builds `<candidate>\nWants: ${JSON.stringify(preferences)}` (**compact JSON, no spaces**), adds `\nThis search: ${focus}` when focus is set, then the sections:
  - `Experience:`: the first 6 work items, as `- ${position} at ${company} (${startDate ?? "?"}–${endDate ?? "present"})`. When there are highlights, `\n  ` plus the first 3 highlights joined with `\n  `.
  - `Education:`: `- ${studyType ?? ""} ${area ?? ""} at ${institution} (${endDate ?? "?"})`.
  - `Skills: ` names joined with `, `.
  - Each empty section renders as `(none on file)`.
  - It closes with `</candidate>`. The dash between dates is U+2013.
- `instructions = RUBRIC + "\n\n" + candidate`, which is identical for every posting in the run (the prompt-cache prefix).
- `prompt` (fast-score.ts:183-190):

  ```
  <posting>
  Title: ${title}
  Company: ${company}
  Location: ${locations.join(" | ") || "unstated"} (${remote})
  Pay: ${salaryMin ? `${salaryMin}–${salaryMax}` : "unstated"}

  ${description_text?.slice(0,4000) ?? "(no description)"}
  </posting>
  ```

  `salaryMin` and `salaryMax` are Postgres numeric values rendered as strings; a null max renders as the literal `null`.

Output schema: `FastScore` (section 14). Reasoning is `reasoningFor("score")`.

`scoreMatches({userId, preferences, focus?, resume, jobIds, threshold?, context?, shouldStop?, onProgress?}) -> {scored, archived, failed}` (fast-score.ts:111-249):

1. `pending = job_matches where user_id = $1 and job_id in jobIds and fast_score IS NULL`. Already-scored matches are skipped ("easiest way to double a run's bill").
2. For each batch of 40:
   - When `shouldStop()` is true, stop (break).
   - Load the postings (id, title, company_name, locations, remote, salary_min, salary_max, description_text).
   - Run `pooled(postings, worker, 16)`.
3. The worker calls `generateStructured("score", FastScore, {instructions, prompt}, context)`. Then `below = score < threshold`, and:

   ```
   UPDATE job_matches
   SET fast_score = score, fast_eval = value(jsonb),
       [if below: stage = 'archived', stage_changed_at = now(),
        notes = "Auto-archived: scored <s>, below the <t> threshold."],
       updated_at = now()
   WHERE user_id AND job_id AND stage = 'found'
   ```

   - Never drag a job the user acted on back to archived.
   - **Gotcha:** if the stage is not `found`, nothing is written, not even `fast_score`, so that match is re-scored on every run. The outcome still counts as scored, and as archived when below the threshold.
   - Any error returns `{score: null, error}`. The posting stays unscored and visible.
4. Counters: `failed` counts errors; `scored` counts successes; `archived` counts successes below the threshold. `onProgress(done, total)` runs after each batch.

`scoreSummary(userId)` returns counts: scored (`fast_score not null`), strong (`>= 80`), good (`65-79`), and archived (stage) (fast-score.ts:252-264).

`pooled(items, worker, concurrency = 6)` (src/lib/pool.ts:8-27) runs N workers pulling from a shared cursor and returns results in input order.

---

## 13. Ranking: deep evaluation, `src/lib/ranking/deep-eval.ts` (LLM call #3, purpose `evaluate`)

Constants:

- `MAX_DESCRIPTION_CHARS = 12000` (deep-eval.ts:39)
- `CALL_TIMEOUT_MS = 4 min`, passed as the `timeout` arg (deep-eval.ts:45)
- `EVAL_OUTPUT_TOKENS = 1800` (deep-eval.ts:432). Measured 2026-09-24 on gpt-5.6-terra at medium effort: 1,370-2,126 output tokens and $0.019-0.030 each. It is set above the average so quotes do not come in low.
- `SCHEMA_OVERHEAD_TOKENS = 400`, `CHARS_PER_TOKEN = 4`, `POSTING_FRAME_CHARS = 400`
- `TYPICAL_EVAL_INPUT_CHARS = RUBRIC.length + 6500 + 7000`, which is 17028 today (the rubric is 3528 chars)

Prompts:

- `RUBRIC` at deep-eval.ts:47-104, static. It covers: bands strong 85-100, apply 70-84, maybe 50-69, skip 0-49; requirements and evidence rules; the seniority, location and salary fit definitions; second-person writing; and "ids only in fact_ids".
- `evalInstructions(candidate)` (deep-eval.ts:139-149) returns `RUBRIC` + `\n\n<candidate>\nWhat they are looking for:\n${describePreferences(preferences)}` + (when focus is set, `\n- this search: ${focus}`) + `\n\nResume (each item's [id] is what fact_ids cite):\n${resumeText}\n</candidate>`.
- `evalPrompt(posting, now)` (deep-eval.ts:152-169):

  ```
  <posting>
  Title: ...
  Company: ...
  Location: ${locations.join(" | ") || "unstated"} (${remote})
  Employment type: ${employmentType ?? "unstated"}
  Pay: ${formatPay}
  Posted: ${formatPosted}
  Today: ${now ISO date}

  ${description ? description.slice(0, 12000) : "(no description)"}
  [\n[Description cut at 12,000 characters.] when truncated]
  </posting>
  ```

  The description is trimmed first.
- `formatPay` (deep-eval.ts:171-185):
  - `toNumber` uses `Number()` and `isFinite`.
  - If both min and max are null, it returns `unstated`.
  - A range uses `toLocaleString("en-US")` with an en dash when min and max differ. Otherwise a single value.
  - Then `${range} ${currency ?? ""} per ${period ?? "unstated period"}` with whitespace collapsed.
- `formatPosted`: `unknown`, or `YYYY-MM-DD (N day|days ago)` with `N = max(0, floor(diff / 86400000))` (deep-eval.ts:187-194).

Output schema: `DeepEval` (section 14). Reasoning is `reasoningFor("evaluate")`, default `medium`.

Post-processing, `sanitizeDeepEval(value, validIds)` (deep-eval.ts:218-248):

- `matched_requirements[].fact_ids` = unique of (`fact_ids` plus UUIDs found in brackets in `evidence`), each with whitespace and a leading `[` or trailing `]` stripped, filtered to `validIds`.
- `requirement`, `evidence`, `gap`, `note` and `summary` go through `stripFactIds`.
- `risk_flags` and `talking_points` go through `stripFactIds`, and empty strings are dropped.
- `BRACKETED_UUIDS = /\s*\[\s*[0-9a-f]{8}-[0-9a-f-]{27,}(?:\s*[,;]\s*[0-9a-f]{8}-[0-9a-f-]{27,})*\s*\]/gi` and `UUID_IN_TEXT = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi` (deep-eval.ts:251-253).
- `stripFactIds(text, validIds)` (deep-eval.ts:267-280): remove `BRACKETED_UUIDS`; for each valid id present as `[id]`, remove `\s*\[<escaped id>\]` globally; collapse 2+ spaces to 1; trim.

Functions:

- `runDeepEval({candidate, posting, context?, overrides?, now?})` (deep-eval.ts:283-303) calls `generateStructured("evaluate", DeepEval, {instructions, prompt, timeout: 240000})` and returns the sanitized result.
- `NoResumeError` has the message "There is no resume on file to evaluate against. Upload one on the profile page first." (deep-eval.ts:305-312).
- `loadEvalCandidate(userId, {brief?})` (deep-eval.ts:319-343):
  - It loads the base resume (`resumes` where kind='base', highest version, which must parse against `ResumeSchema`) and the profile.
  - It returns null when there is no base resume.
  - Otherwise `{resumeId, resumeVersion, resumeText: resumeToText(resume), preferences: applyBrief(parsed prefs or EMPTY, brief ?? EMPTY_BRIEF), focus: brief?.focus ?? null, factIds: Set(factIds(resume))}`.
  - Why the brief applies (deep-eval.ts:333-335): an entry-level role found by an entry-level search must not be marked too junior.
- `evaluateMatch({userId, matchId, context?, candidate?, overrides?})` (deep-eval.ts:359-411):
  1. The candidate comes from the argument or from `loadEvalCandidate`; with none, it throws `NoResumeError`.
  2. It selects job fields joined via job_matches where `user_id` and `match.id` match. With no row, it throws "That match does not exist."
  3. It runs `runDeepEval` with `context ?? {userId}`.
  4. It calls `saveDeepEval(userId, matchId, evaluation)` (queries/matches.ts:536-561), which runs:

     ```
     UPDATE job_matches
     SET deep_score = score, deep_eval = evaluation, updated_at = now(),
         [if recommendation = 'skip':
            stage = CASE WHEN stage = 'found' THEN 'archived' ELSE stage END,
            stage_changed_at likewise,
            notes = CASE WHEN stage = 'found'
                         THEN concat_ws(E'\n\n', nullif(notes, ''),
                              'Archived: the deep review recommended skipping this one.')
                         ELSE notes END]
     WHERE user_id AND id
     RETURNING *
     ```

  5. When nothing was returned, it throws "That match was removed during evaluation."
  6. It returns `{matchId, jobId, title, company, evaluation}`.
- `estimateEvalCost(n, {inputChars?, modelRef?, at?})` (deep-eval.ts:453-479):
  - `ref = modelRef ?? modelRef("evaluate")`; if that throws, it returns `{usd: null, perItemUsd: null, model: "unconfigured"}`.
  - When the ref is unpriced, `{null, null, model: ref}`.
  - Otherwise `inputTokens = ceil((inputChars ?? 17028) / 4) + 400` and `perItem = costUsd(ref, {inputTokens, outputTokens: 1800}, at)`.
  - Returns `{usd: round4(perItem * floor(max(0, n))), perItemUsd: round4(perItem), model}`, where `round4` is `Math.round(x * 1e4) / 1e4`.
  - Prompt caching is ignored, so the estimate errs high.
- `estimateEvalCostFor(candidate, descriptionChars[], opts)` computes the average of `evalInstructions(candidate).length + 400 + min(chars, 12000)` and calls `estimateEvalCost(n, {inputChars: avg})` (deep-eval.ts:486-502).
- `costPhrase(usd)`: null, "under a cent" when below 0.01, otherwise `about $X.XX` (deep-eval.ts:505-509).
- Consumers:
  - search-run.ts:387-402 (approval quote)
  - evaluate-run.ts:274, 605, 714 (`EVAL_CONCURRENCY = 5`, evaluate-run.ts:67)
  - actions/matches.ts:104-123
  - agents/tools/matches.ts:106
  - settings page (`estimateEvalCost(1)`)

---

## 14. Ranking schemas, `src/lib/ranking/schemas.ts` (field level)

Every field is required; absence is written as null or an empty array, because of OpenAI strict mode and because cheap models omit optional fields (schemas.ts:3-9).

- `Seniority` (ranking): `intern|new_grad|entry|mid|senior|staff_plus|unclear` (schemas.ts:11-19). This is different from the profile `Seniority`, which has no `unclear`.
- `FastScore` (schemas.ts:26-41):
  - `score`: int, 0-100
  - `fit_reasons`: string[] (at most three, short; enforced by the prompt only)
  - `red_flags`: string[]
  - `seniority`: the ranking Seniority above
  - `hard_requirement_misses`: string[]
- `Recommendation`: `strong|apply|maybe|skip` (schemas.ts:46).
- `MatchedRequirement`: `{requirement: string, evidence: string, fact_ids: string[], strength: "strong"|"partial"}` (schemas.ts:49-57).
- `EvalGap`: `{gap: string, severity: "learnable"|"disqualifying", note: string}` (schemas.ts:61-69).
- `DeepEval`, **in this field order** (the order is deliberate: evidence before verdict anchors the score, schemas.ts:73-86):
  - `matched_requirements: MatchedRequirement[]`
  - `gaps: EvalGap[]`
  - `seniority_fit: under|match|over|unclear`, with `.describe("The candidate against the posting's level: under = the role asks for more experience than they have; over = they are overqualified.")`
  - `location_fit: match|partial|mismatch|unclear`
  - `salary_fit: above|within|below|unstated`, with `.describe("The posting's pay against the candidate's salary floor: above = pays more than their floor; below = pays less.")`
  - `risk_flags: string[]`
  - `talking_points: string[]`
  - `summary: string`
  - `score: int 0-100`
  - `recommendation: Recommendation`
- `job_matches.fast_eval` and `deep_eval` (jsonb) store these objects exactly. The tailor reads them leniently (section 18).

---

## 15. Bake-off fixture and scripts

- `src/lib/ranking/bakeoff-fixture.ts`:
  - A static simplified `RUBRIC` (bakeoff-fixture.ts:27-30) and a hard-coded `CANDIDATE` (bakeoff-fixture.ts:32-38).
  - `scoringSample(limit = 20)` selects jobs where `description_text is not null and length between 1500 and 12000`, ordered by id, with the limit (bakeoff-fixture.ts:48-64).
  - `runScoringFixture(modelRef, {reasoning?, limit?})` (bakeoff-fixture.ts:80-153):
    - It uses `registry.languageModel(modelRef)` **without the ledger middleware**, so bake-off calls are **not ledgered**.
    - It calls `generateStructured("score", FastScore, ..., {}, {model, reasoning, providerId: provider})` sequentially.
    - The prompt is the posting without the Pay line, with the description sliced to 4000.
    - Token totals use `tokensFromProviderUsage` built from the `ai`-level usage (`inputTokenDetails.noCacheTokens`, `cacheReadTokens`, `cacheWriteTokens`; `outputTokenDetails.reasoningTokens`).
    - Every exception counts as a schemaFailure.
  - `scoreAgreement(candidate, reference)` is `max(0, 1 - mean(|diff|) / 100)` over shared job ids, or 0 when none are shared (bakeoff-fixture.ts:162-176).
- `scripts/model-bakeoff.ts`:
  - CLI: `--purpose`, `--models a:b,c:d`, `--reference`, `--reasoning`. Only the `score` fixture exists.
  - It prints a table: cost, `per500 = cost / total * 500`, latency, reasoningShare, schemaFail, agreement.
  - It warns about unpriced models.
- `scripts/llm-smoke.ts`:
  - For each configured purpose (optionally filtered by argv), it calls `generateStructured(purpose, {city: string.describe(...), confident: boolean}, {instructions: "Answer factual questions. Respond only with the requested fields.", prompt: "Which city is the Golden Gate Bridge in?"})`.
  - It prints tokens, cached tokens and retried, and flags unpriced models.
  - It exits 1 on any failure.

---

## 16. Resume schema and text, `src/lib/resume/schema.ts` and `text.ts`

### 16a. Storage schema `ResumeSchema` (schema.ts:86-93)

This is stored in `resumes.data` (jsonb) and mirrored to `profiles.data`.

- `ItemSource` is `resume|linkedin_pdf|user`; `user` always wins a merge (schema.ts:13).
- `DateString` is a string of 1-40 characters, kept **verbatim** and never normalized (schema.ts:16-21).
- Every item has `id: string` and `source: ItemSource` with default `resume` (schema.ts:23-27).
- `Basics`: `{name: string, default ""}`, `headline`, `email`, `phone` and `location` (each nullish), and `links: [{id, source, label, url}]` with default `[]`.
- `WorkItem`: `{id, source, company, position, location?, startDate?, endDate?, summary?, highlights: string[] = []}`.
- `EducationItem`: `{id, source, institution, area?, studyType?, startDate?, endDate?, score?, highlights = []}`.
- `ProjectItem`: `{id, source, name, description?, url?, startDate?, endDate?, highlights = []}`.
- `SkillItem`: `{id, source, name, category?}`. The category is the resume's own grouping and is never inferred.
- Top level: `basics` (default `{name: "", links: []}`), `summary?`, and `work`, `education`, `projects`, `skills`, each defaulting to `[]`.

### 16b. LLM extraction schema `ExtractedResumeSchema` (schema.ts:157-171)

Written by hand: everything is required and nullable, and there are no ids or sources (schema.ts:101-114).

- `basics`: `{name: string, headline|null, email|null, phone|null, location|null, links: [{label, url}]}`
- `summary|null`
- `work: [{company, position, location|null, startDate|null (1-40), endDate|null (1-40), summary|null, highlights: string[]}]`
- `education: [{institution, area|null, studyType|null, startDate|null, endDate|null, score|null, highlights}]`
- `projects: [{name, description|null, url|null, startDate|null, endDate|null, highlights}]`
- `skills: [{name, category|null}]`

The date length limits are validated locally; a violation produces TypeValidationError and a retry.

### 16c. Helpers

- `withIds(extracted, source, newId = randomUUID)` stamps every link, work, education, project and skill item with a fresh uuid and the `source`, then `ResumeSchema.parse` (schema.ts:176-198).
- `factIds(resume)`: the ids of `basics.links`, then work, education, projects and skills, in that order (schema.ts:201-209).

### 16d. `resumeToText(resume, sections = RESUME_SECTIONS)` (text.ts:23-117)

This is the prose every model reads. `RESUME_SECTIONS = basics, summary, work, education, projects, skills`. Parts are joined with `\n\n`.

- **basics**: lines `Name: <name || "(unknown)">`, then `Headline: ...`, `Location: ...` and `Email: ...` when present, then `Links: <label url, ...>` when any exist. Phone is **not** included.
- **summary**: `SUMMARY\n<summary>`.
- **work**: `EXPERIENCE\n` then roles joined with `\n\n`. Each role:
  - `[id] Position — Company` plus ` (location)` when present
  - `  start ?? "?" – end ?? "present" · source: <source>`
  - `  <summary>` when present
  - `  • <highlight>` for each highlight
- **education**: `EDUCATION\n` then one line per item: `[id] ${studyType ?? ""} ${area ?? ""} — institution (start ?? "?" – end ?? "?")`, plus ` · score` when present. Double spaces are possible and kept verbatim. Education highlights are not included.
- **projects**: `PROJECTS\n` then `[id] name` plus ` — url`, then `  description` and `  • h` lines, joined with `\n\n`.
- **skills**: `SKILLS\n` then `Category: a, b` lines, grouped in order of first appearance, with a null category becoming `Other`.

Separators are an em dash (U+2014) between position and company, an en dash (U+2013) between dates, and a middle dot (U+00B7).

---

## 17. Resume extraction (LLM calls #4 and #5, purpose `extract`)

### 17a. `src/lib/resume/extract.ts`

- Prompts:
  - `INSTRUCTIONS` (extract.ts:23-50), 8 numbered rules: never invent; copy verbatim; keep date formats; reassemble interleaved columns; keep skill categories; headings are not content; close letter-spacing ("S U M M A R Y"); rejoin hyphenated line breaks.
  - `VISION_INSTRUCTIONS = INSTRUCTIONS + "\n7. You are reading page images because ... omit the item rather than guessing at it."` (extract.ts:52-55). The rule number 7 is duplicated; copy the text verbatim.
- Signature: `extractResume({data: Buffer, mime, source = "resume", context = {}, forceVision?}) -> {resume: Resume, text, usedVision, garble, retried}` (extract.ts:67-107).
- Steps:
  1. A PDF (`mime === "application/pdf"`) goes through `parsePdf`. Anything else goes through `parseDocx`, with `pageCount: 1`.
  2. `useVision = isPdf && (forceVision || garble.garbled)`. When garble alone triggered it, it logs `[resume] text layer unusable, falling back to page images: <reasons joined "; ">`. DOCX has **no** vision fallback.
  3. Text path (`extractFromText`, extract.ts:111-121): `generateStructured("extract", ExtractedResumeSchema, {instructions: INSTRUCTIONS, prompt: "Transcribe this resume.\n\n<resume>\n${text}\n</resume>"}, context)`.
  4. Vision path (`extractFromImages`, extract.ts:123-147): `images = renderPdfPages(data)`, then `messages: [{role: "user", content: [{type: "text", text: "Transcribe this resume."}, ...images.map(png => ({type: "file", mediaType: "image/png", data: Buffer}))]}]` with `instructions: VISION_INSTRUCTIONS`.
  5. It returns `withIds(value, source)`, the raw text, `usedVision`, `garble` and `retried`.
- `forceVision` has no caller today.
- Reasoning is `reasoningFor("extract")`, default `low`. The extract model must be vision-capable for the fallback (.env.example:34-35).

### 17b. `parse/pdf.ts`

- `parsePdf(data)` uses `unpdf.extractText(bytes, {mergePages: true})` and returns `{text, pageCount: totalPages, garble: detectGarble(text, totalPages)}` (pdf.ts:19-28). unpdf's merge (unpdf dist/index.mjs:383-401):
  - Per page, pdf.js `getTextContent().items` are joined as `item.str + (hasEOL ? "\n" : "")`.
  - Pages are joined with `\n`.
  - Then `/[^\S\n]+/g` becomes a single space, `/ ?\n ?/g` becomes `\n`, and `/\n{3,}/g` becomes `\n\n`.
- `MAX_VISION_PAGES = 4` (pdf.ts:31).
- `renderPdfPages(data, maxPages = 4)` renders pages 1 to `min(numPages, 4)` with `renderPageAsImage(doc, page, {scale: 2, canvasImport: @napi-rs/canvas})` and returns a list of PNG Buffers (pdf.ts:39-58). Scale 2 is the cheapest setting at which 9-10pt text stays legible; below that the model invents dates (pdf.ts:33-38).

### 17c. `parse/docx.ts`

- `parseDocx(data)` uses `mammoth.extractRawText({buffer})` and returns `{text: value, garble: detectGarble(value, 1)}` (docx.ts:10-19).

### 17d. `parse/garble.ts`, `detectGarble(text, pageCount) -> GarbleReport`

Thresholds (garble.ts:31-40):

| constant | value |
|---|---|
| `MIN_CHARS_PER_PAGE` | 180 |
| `MIN_SPACE_RATIO` | 0.08 |
| `MIN_ALPHA_RATIO` | 0.5 |
| `MAX_REPLACEMENT_RATIO` | 0.01 |
| `MAX_SINGLE_CHAR_WORD_RATIO` | 0.4 |

`BROKEN_ENCODING = /[�\u0000-\u0008\u000B\u000C\u000E-\u001F]/g` (garble.ts:43-46).

Metrics (garble.ts:48-68):

- `chars = text.length` (UTF-16 units)
- `pages = max(pageCount, 1)`
- `spaces = count(/\s/)`
- `alpha = count(/\p{L}/u)`
- `replacement = count(BROKEN_ENCODING)`
- `words = split(/\s+/)` with empty strings filtered
- `singleCharWords`: words of length 1
- `nonSpace = max(chars - spaces, 1)`
- `charsPerPage = chars / pages`
- `spaceRatio = chars ? spaces / chars : 0`
- `alphaRatio = alpha / nonSpace`
- `replacementRatio = chars ? replacement / chars : 0`
- `singleCharWordRatio = words ? single / words : 0`

Reasons, verbatim strings (garble.ts:70-93):

- `only ${round(charsPerPage)} characters per page — likely a scan or an image-only PDF` when charsPerPage < 180.
- `word spacing lost (${spaceRatio.toFixed(3)} of characters are whitespace)` when chars > 0 and the ratio is below 0.08.
- `mostly non-letters (${alphaRatio.toFixed(2)} alphabetic)` when chars > 0 and the ratio is below 0.5.
- `replacement or control characters — broken font encoding` when the ratio is above 0.01.
- `text is letter-spaced (${(ratio * 100).toFixed(0)}% single-character words)` when the ratio is above 0.4.

`garbled = reasons.length > 0`. The detector deliberately does **not** detect wrong reading order; the prompt handles that (garble.ts:10-15).

Test fixtures: src/test/fixtures/resumes/{single-column,two-column,serif-dense}.pdf and plain.docx.

### 17e. Call site: `POST /api/uploads` (src/app/api/uploads/route.ts:24-116)

- Extraction runs **inline in the request**, not as a job. The user is watching a spinner and it is one LLM call (route.ts:17-23).
- `MAX_BYTES = 10 MB`, returning 413 when exceeded.
- Only `application/pdf` and the `.docx` mime type are accepted; anything else gets 415. A missing file gets 400.
- The file is stored before extraction. `context: {userId}` has no runId.
- On failure it returns 422 `{error: "Could not read that resume: <msg>"}`.
- On success it returns `{documentId, resumeId, usedVision, garble, counts: {work, education, skills, projects}}`.
- `linkedin_pdf` is parsed but not saved as the base resume (route.ts:79-88).

---

## 18. Tailoring, `src/lib/resume/tailor.ts` (LLM call #6, purpose `tailor`, reasoning default `high`)

Constants: `MAX_POSTING_CHARS = 10000` (tailor.ts:53) and `TAILOR_SECTIONS = summary|work|projects|education|skills` (tailor.ts:25-31).

`TailorChange` (stored in `resumes.changes`): `{section, item_id|null, before|null, after|null, reason, evidence_fact_ids: string[]}` (tailor.ts:39-50).

Output schema `TailorOutputSchema` (tailor.ts:67-116). Every field is required and nullable. Items carry the base `id`. There are no basics. Skills are names only.

- `summary`: string or null
- `work: [{id, company, position, location|null, startDate|null, endDate|null, summary|null, highlights[]}]`
- `education: [{id, institution, area|null, studyType|null, startDate|null, endDate|null, score|null, highlights[]}]`
- `projects: [{id, name, description|null, url|null, startDate|null, endDate|null, highlights[]}]`
- `skills: string[]`
- `changes: [{section: enum TAILOR_SECTIONS, item_id|null, before|null, after|null, reason, evidence_fact_ids[]}]`

Prompt:

- `INSTRUCTIONS` at tailor.ts:120-144, static: editor not author; select, reorder, rephrase; summary rules; the never-do list (no new facts, no computed numbers, no moving facts, no inflated ownership, no claiming Gaps); copy exact fields; length guidance; change-log rules.
- `buildTailorPrompt({base, job, notes?, preferences?})` (tailor.ts:245-302):
  - `instructions` = `INSTRUCTIONS + "\n\n<resume>\n" + resumeToText(base) + "\n</resume>\n\n<exact_fields>\n" + JSON.stringify(exactFields(base), null, 1) + "\n</exact_fields>"`. The indent is **1 space**. This stable prefix is for prompt caching across jobs.
  - `exactFields(base)` (tailor.ts:202-231):
    - `work: [{id, company, position, location ?? null, startDate ?? null, endDate ?? null}]`
    - `education: [{id, institution, area, studyType, score, startDate, endDate (all ?? null), highlights}]`
    - `projects: [{id, name, url, startDate, endDate}]`
    - `skills: [names]`
    - Why (tailor.ts:197-201): `resumeToText` renders missing dates as "?" or "present", which a copier would reproduce.
  - `prompt` = parts joined with `\n\n`:
    1. `Tailor the resume for this posting.`
    2. `<posting>\nTitle: ..\nCompany: ..\nLocation: <locations " | " or "unstated"> (<remote>)\n\n<postingText(job)>\n</posting>`
    3. When any evaluation notes exist: `<evaluation>\n` + sections joined with `\n\n` + `\n</evaluation>`. The sections are `What the posting asks for that the candidate has:\n- ...`, `Worth emphasising:\n- ...`, and `Gaps — the candidate does NOT have these; never claim or imply them:\n- ...`.
    4. When goals exist: `<candidate_goals>\nTarget roles: a, b\nSeniority: x\n</candidate_goals>`.
- `postingText(job)` = `descriptionText.trim() || htmlToText(descriptionRaw) || ""`, sliced to 10000, or `(no description)` (tailor.ts:233-238). `htmlToText` lives in `sources/clean.ts` (sources area).
- `evaluationNotes(match)` reads the evaluations leniently (tailor.ts:157-189):
  - `matched` from `deep_eval.matched_requirements`: a string is taken as-is; otherwise `"<requirement> — shown by: <evidence>"`, or whichever of the two exists.
  - `gaps` from `deep_eval.gaps` (`"<gap> (<note>)"` or `gap`) plus `fast_eval.hard_requirement_misses`, de-duplicated and capped at 12.
  - `talkingPoints` from `deep_eval.talking_points`.
  - `list()` caps each list at 10 items and truncates each item over 300 chars to 297 + `…` (tailor.ts:514-521).

`mapTailoredOutput(base, output) -> {resume, changes, dropped: MappingDrop[]}` (tailor.ts:331-471):

- Basics are a `structuredClone` of the base.
- `anchor` keeps only items whose trimmed id is a base id, skipping a repeated id. Unknown ids go to `dropped` with reason `Not a role|an education entry|a project on your resume, so it was removed.`.
- Work and education are re-sorted into base order; projects keep the model's order.
- `source` comes from the base item.
- Strings are trimmed. `nullable()` turns a blank into null. `clean()` trims highlights and drops blanks.
- `date(value, original)`: if the trimmed value is longer than 40, the base value is used. The storage cap is 40, and failing the parse is worse.
- Skills match base skills by `skillKey` (`NFKC → lower → collapse whitespace → trim`). The base item is cloned, keeping its id, source and category. Unknown names go to `dropped` (`<name> is not a skill on your resume, so it was removed.`), and duplicates are skipped.
- `ResumeSchema.parse(...)`.
- `changes` are passed through with trims and nullables; `evidence_fact_ids` is cleaned.

`tailorResume({base, job, match?, preferences?, context?})` calls `generateStructured("tailor", TailorOutputSchema, {instructions, prompt}, context)` and returns `{...mapTailoredOutput, retried}` (tailor.ts:480-502).

---

## 19. Verification, `src/lib/resume/verify.ts` (LLM call #7, purpose `verify`, reasoning default `low`)

Two passes: deterministic rules, then one model call over the rewritten lines that passed the rules. One deterministic revision follows, then the rules are re-checked (verify.ts:10-30).

### 19a. Types

- `UnsupportedClaim {section, item_id|null, text, reason, source: "rule"|"model"}`
- `Revision = UnsupportedClaim + {action: replaced|dropped|reverted, replacement|null}`
- `Verification` (stored in `resumes.verification`): `{passed, checkedAt ISO, unsupported[], revised, checked, modelChecked, revisions[], modelError|null, baseResumeId|null, baseVersion|null}` (verify.ts:49-68)
- `Target`: `basics{field}`, `item{section, itemId}`, `field{section, itemId, field}`, `skill{index}`, or `text{key}` (verify.ts:73-78)
- `Finding = UnsupportedClaim + {target}`

### 19b. Claims

- `normalizeText(v)` (verify.ts:99-109): NFKC, lower, curly single quotes to `'`, curly double quotes to `"`, `‐‑‒–—` to `-`, collapse whitespace, trim, strip a trailing `[.;,]+`.
- `splitSentences(text)`: split on `/(?<=[a-z0-9%)"'’”][.!?])\s+(?=[A-Z"“(])/`, trim, drop empties (verify.ts:117-122).
- `collectClaims(base, tailored)` (verify.ts:124-205):
  - Summary sentences: key `summary:sentence:<i>`, field `sentence`. `verbatim` is true when the whole summary equals the base summary or the sentence is a base sentence.
  - Work: summary `work:<id>:summary:0`, and highlights `work:<id>:highlight:<i>`, verbatim when present in the base highlights after normalization.
  - Projects: description `projects:<id>:description:0`, plus highlights.
  - Education: highlights only.

### 19c. Extractors

- `URL_PATTERN` (verify.ts:209-210) is a long TLD list with flags `gi`. `EMAIL_PATTERN = /[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}/gi`.
- `extractLinks`: emails plus URLs (emails stripped first), normalized by lowercasing, stripping the scheme and `www.`, and stripping trailing `/.,;:)`, then de-duplicated (verify.ts:217-230).
- `extractMetrics(text)` (verify.ts:314-355) works on text with links stripped:
  - Digits: `(?<![\p{L}\p{N}_.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)`. A number glued to a letter ("S3", "EC2") is a name, not a quantity.
  - The first matching `SUFFIXES` entry (verify.ts:301-312) on the remaining text maps the value to keys: `%` or percent gives `pct:v`; `x`, `×` or `fold` gives `x:v`; `times` gives `x:v` and `n:v`; `k` or thousand gives `n:v*1e3`; million, mil or mn gives `n:v*1e6`; case-sensitive `M` or `MM` gives `n:v*1e6`; lower-case `m` or `mm` gives `n:v*1e6` and `n:v` (ambiguous); billion or bn gives `n:v*1e9`; case-sensitive `B` gives `n:v*1e9`. With no suffix the key is `n:v`.
  - `fmt(v) = String(Number(v.toPrecision(12)))`.
  - Number words: tens-units pairs ("twenty-five"); `NUMBER_WORDS` (2-19, the tens, hundred, dozen; "one" is excluded because it is usually "one of"); `MULTIPLIER_WORDS` (twice/doubled/doubling/twofold give 2, tripled/tripling/threefold give 3, quadrupled/fourfold give 4, fivefold 5, tenfold 10) give `x:N`.
- `extractTerms(text)` (verify.ts:369-389): tokens `/[\p{L}\p{N}][\p{L}\p{N}.+#'’/-]*/gu`, with a trailing possessive `'s` and trailing `.-/'` stripped, split on `/` and `-`, and each part's dots trimmed. A part is kept when it is at least 2 long, does not start with a digit, contains a letter, and is mixed-case after its first character, has a digit or `.+#` after its first character, or starts with a capital and is not the first part of a sentence-initial token. `isSentenceStart` checks that the trimmed text before the token is empty or ends with `.!?:;•`.
- `ALIASES` (verify.ts:397-415): postgres and postgresql, k8s and kubernetes, js→javascript, ts→typescript, golang→go, node/nodejs/node.js, react/reactjs/react.js, next/next.js, vue/vue.js.
- `termIn(term, scopeLower)`: variants are the lowercased term, its aliases, minus a trailing `s` when longer than 3, and minus `es` when longer than 4. Matching uses `containsWord` with letter and number boundaries (verify.ts:417-437).
- Scopes (verify.ts:441-469):
  - Each work, education or project item's scope is all of its string values except id and source, joined with `\n`, plus its metric keys.
  - The resume scope is all strings in the whole resume.
  - A bullet may only draw on its own item; the summary may draw on the whole resume.

### 19d. `checkRules(base, tailored) -> Finding[]` (verify.ts:519-652)

All reason strings are user-facing and must be copied verbatim.

1. **Basics**: `name|email|phone|location|headline`, trimmed (blank counts as null), must equal the base: `Your <field> must match your resume exactly.` Links compare as a JSON list of `[label.trim(), url.trim()]`: `Your links must match your resume exactly.`
2. **Items** not in the base: `Not a <role|education entry|project> on your resume.`, target `item`.
3. **Strict fields**:
   - Work: company, position, startDate, endDate.
   - Education: institution, area, studyType, startDate, endDate.
   - Projects: name, url, startDate, endDate.
   - Omittable fields, which may be removed but not changed: work `location` and education `score`.
   - Messages: `Removed the <label> "<was>".`, `Added a <label> ("<now>") that is not on your resume.`, `Changed the <label> from "<was>" to "<now>".`
   - `FIELD_LABELS` is at verify.ts:486-498.
4. **Skills** not in the base (by NFKC lowercase name): `<name> is not a skill on your resume.`, target `skill{index}`.
5. **Non-verbatim claims**:
   - numbers whose keys are not in the scope's metric set
   - links not in the normalized resume text corpus (lowercased, schemes and `www.` stripped)
   - terms not in the scope
   - Messages: `Mentions <listPhrase(problems)>, which isn't|aren't in this <role|...> on your resume|your resume.` and `Links to <...>, which isn't|aren't on your resume.`, joined with a space.
   - `listPhrase` takes the first 4 items as `a, b and c`.

### 19e. Model judge (`modelJudge`, verify.ts:740-790)

- `JUDGE_INSTRUCTIONS` is at verify.ts:664-680.
- `instructions` = `JUDGE_INSTRUCTIONS + "\n\n<original_resume>\n" + resumeToText(base) + "\n</original_resume>"`.
- Claim ids are `c1..cN`.
- `origins` maps `normalizeText(change.after)` to `change.before`, from the tailor's change log.
- Claims are grouped by `section:itemId`. Each group renders as:

  ```
  <item section="S" [id="ID"]>
  <original>
  {itemEvidence}
  </original>
  <claim id="cK">
  text
  [<rewritten_from>before</rewritten_from>]
  </claim>
  ...
  </item>
  ```

- Groups are joined with `\n\n`. The prompt is `Check these claims.\n\n` + the blocks.
- `itemEvidence` (verify.ts:703-738):
  - work: `position — company (location)`, then dates joined with ` – `, summary, and `• h` lines
  - project: `name — url`, description, `• h`
  - education: `studyType, area, institution`, dates, score, `• h`
  - no item: `(the whole original resume, above)`, or `(no <section> item with this id)` when the id is unknown
- Schema `Verdicts`: `{verdicts: [{claim_id: string, supported: boolean, reason: string|null}]}` (verify.ts:682-690).
- Verdicts map back to claim keys by the trimmed `claim_id`; unknown ids are ignored.

### 19f. `verifyTailored(base, tailored, changes, context, deps = {judge?})` (verify.ts:805-856)

1. `findings = checkRules`.
2. `toJudge` = claims that are not verbatim and not already flagged by a text rule.
3. When `toJudge` is non-empty, call the judge. For each claim:
   - `supported` means it passes.
   - Otherwise it becomes a finding with the reason `verdict.reason.trim() || "Not supported by your resume."`, or `The check returned no verdict for this line, so it was not trusted.` when no verdict came back.
   - If the judge **throws**, `modelError = message` and every `toJudge` claim gets `Could not be checked, so your original wording was kept.` (source `model`). An unchecked rewrite is not a verified one.
4. Returns `{findings, checked: claims.length + tailored.skills.length, modelChecked: toJudge.length, modelError}`.

### 19g. `reviseTailored(base, tailored, findings) -> {resume, revisions}` (verify.ts:927-1096)

It is deterministic and only ever moves toward the base.

1. Basics are reverted (links are cloned from the base). The revision is `reverted` with the base value.
2. Unknown items are filtered out and recorded as `dropped`.
3. Changed fields are reverted to the base value (or null) and recorded as `reverted`.
4. Unknown skills are removed by index and recorded as `dropped`.
5. Text, keyed by claim key with the first finding per key:
   - **Summary**: flagged sentences are dropped. If some survive, they are joined with a space. If none survive, the base summary is used and the last revision becomes `replaced` with the base summary.
   - **Bullets**: for a flagged bullet, find the unused original with the highest `similarity` (at least `MIN_RESEMBLANCE = 0.15`; ties go to the later original because of `>=`). Use it and record `replaced`, or record `dropped`. Originals already present unflagged are excluded, to avoid duplicates.
   - **Work summary** and **project description**: revert to the base, recorded as `replaced`, or `dropped` when the base has none.
- `similarity(a, b)` is the Dice coefficient over `contentWords`:
  - `normalizeText`, split on `[^\p{L}\p{N}.%]+`, strip trailing dots.
  - Keep words longer than 2 that are not in `STOPWORDS` (verify.ts:871-873).
  - Apply the crude `stem` (suffixes ments, ment, ions, ion, ing, ies, ed, es, ly, s, keeping a stem of at least 3).

### 19h. Finalizing the record

- `finalizeVerification({base, final, result, revisions, baseResumeId?, baseVersion?})` re-runs `checkRules(base, final)`. Then `passed = remaining.length === 0`, `unsupported = remaining`, `revised = revisions.length > 0`, and `checkedAt = now` (verify.ts:1102-1123).
- `reconcileChanges({base, final, changes, revisions})` (verify.ts:1134-1202):
  - Drop entries whose `item_id` is not a base fact id.
  - Filter `evidence_fact_ids` to base ids. When the list ends up empty, use `[item_id]`, or `[]`.
  - Skip summary entries; the summary is re-added at the front when the final summary differs from the base (normalized), with `before` = base, `after` = final, and the logged reason or `Rewritten for this posting from facts on your resume.`.
  - Drop entries whose `after` was reverted or is no longer present (except skills).
  - Append entries for unlogged rewrites (non-verbatim, non-sentence claims) with `before = closestOriginal` and reason `Reworded for this posting.`.

### 19i. Orchestration (runs area): `executeTailorRun` (src/lib/runs/tailor-run.ts:70-221)

1. Load the match and the base resume.
2. `context = {userId, runId}`.
3. Draft with `tailorResume`. When the draft has no work but the base does, fail with "The tailored draft came back without any of your experience, so it was discarded. Try again.".
4. `verifyTailored`.
5. The mapping's `dropped` entries become revisions (source `rule`, action `dropped`).
6. When there are findings, `reviseTailored`.
7. `finalizeVerification`, then `reconcileChanges`.
8. `saveTailoredResume`.
9. Emit `tailor.verified`.

Cancellation is checked after the draft and before the save.

---

## 20. Resume provenance and HTML template (no LLM)

- `stampUserEdits(next, previous)` (src/lib/resume/edits.ts:67-81):
  - With no previous resume, return `next` unchanged.
  - For links, work, education, projects and skills: an item whose id is new, or whose content changed (stable, key-sorted serialization ignoring `id`, `source` and undefined values), becomes `source: "user"`. Otherwise it keeps the previous source.
  - Why the stable serialization (edits.ts:15-23): jsonb does not preserve key order.
  - Consumer: actions/profile.ts:47.
- `renderResumeHtml(resume, {title?, density?: normal|compact})` (src/lib/resume/render/template.ts:276-315) is a pure string function: a full HTML document with inline `STYLES` (template.ts:323-513).
  - Accent `#1E5C45`, Letter size, margins 0.5in by 0.6in.
  - Every interpolated value goes through `escapeHtml` (`& < > " '`). This is stored-XSS defence, because the preview is served from the app's origin.
  - `safeHref` (template.ts:63-75) allows http(s) and mailto, prefixes a bare domain with `https://`, and refuses other schemes.
  - `displayUrl` and `dateRange` (template.ts:78-96).
  - The masthead puts contact on one line, or two lines when the width estimate exceeds `CONTACT_LINE_CHARS = 100` (template.ts:149-186).
  - `phone` becomes a `tel:` link when it has at least 7 digits.
  - Section order: Summary, Experience, Projects, Education, Skills. Empty sections are omitted.
  - Skills are grouped by category in order of first appearance, with uncategorised skills last. When every skill is uncategorised there is a single `skills-line`.
  - Heading letter-spacing stays at 0.07em on purpose. Above about 0.1em, PDF text extraction reads "S U M M A R Y" (template.ts:417-419).
  - Consumers:
    - GET /api/resumes/[id]/preview, with CSP `default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'self'`.
    - worker `renderStoredResume`: render normal; if it runs past 1 page, render compact and keep it when it has fewer pages (worker/handlers/resume.ts:153-157).

---

## 21. Profile (no LLM calls; prompt inputs and gates)

### 21a. `src/lib/profile/interview.ts`

- `INTERVIEW_VERSION = 1`.
- `Seniority`: `intern|new_grad|entry|mid|senior|staff_plus`. `RemotePreference`: `remote|hybrid|onsite|any`. `SalaryFloor`: `{amount: number, currency: string, period: year|hour}` (interview.ts:13-30).
- `PreferencesSchema` (interview.ts:46-56): every field is `.nullable().default(null)`.
  - Fields, in this **order, which matters for JSON prompts**: `target_roles: string[]`, `seniority`, `locations: string[]`, `remote_pref`, `salary_floor`, `work_auth: string`, `start_date: string`, `must_haves: string[]`, `dealbreakers: string[]`.
  - Why the defaults (interview.ts:32-42): old rows missing a key must still parse, or every answer the user gave is silently lost.
- `EMPTY_PREFERENCES` has every field null.
- `SLOTS` (interview.ts:99-162): `{id, label, intent, fields, required}` for each slot.
  - Required: target_roles, seniority, locations, remote_pref, work_auth.
  - Optional: salary_floor, start_date, and constraints (which owns must_haves and dealbreakers).
- `InterviewStateSchema`: `{version = 1, skipped: string[] = []}`.
- `isFilled(slot, prefs)` uses **`some`** field non-null, and a non-empty list when it is an array. The doc comment says "every", but the code (and the test interview.test.ts:80) say either field counts.
- `interviewProgress(prefs, state)` (interview.ts:199-223):
  - `filled`
  - `outstanding`: not filled and not skipped
  - `blocking`: required and not filled, where skipping does not unblock
  - `answered = SLOTS.length - outstanding.length`, `total = 8`, `complete = outstanding.length === 0`
- `describePreferences(prefs)` (interview.ts:226-237) prints, for every slot field in SLOTS order with a non-null or non-empty value, `- <field>: <JSON.stringify(value)>` (compact). When there are none, `- nothing recorded yet`. Consumers: the deep-eval prompt and the chat system prompt.

### 21b. `src/lib/profile/onboarding.ts`

- `getOnboardingState(userId)` (onboarding.ts:31-54):
  - `resume` = `ResumeSchema.safeParse(profiles.data)`.
  - `hasResume = work + education + skills > 0`.
  - Preferences and state are parsed leniently.
  - `settings = parseSettings(profiles.settings)`.
  - `ready = hasResume && blocking.length === 0`.
- Consumers: home, chat, matches and profile pages.

### 21c. `src/lib/profile/settings.ts`

- `SettingsSchema` (settings.ts:18-28):
  - `defaultMode`: `approve_each|autopilot`, default `approve_each`
  - `scoreThreshold`: int 0-100, default 55
  - `deepEvalTopN`: int 0-60, default 15
- `parseSettings(v)` uses `safeParse(v ?? {})`, falling back to the defaults.
- `getSettings(userId)` runs `SELECT settings FROM profiles WHERE user_id`.
- `updateSettings(userId, patch)` merges and re-parses (a parse failure throws), then runs `UPDATE profiles SET settings, updated_at WHERE user_id RETURNING id`. It returns null when there is no profile row.

---

## 22. Every LLM call in scope (and adjacent call sites)

| # | Purpose | Function (file:line) | Instructions (file:line) | Output schema | Default effort | Timeout | Attribution | Caller / trigger |
|---|---|---|---|---|---|---|---|---|
| 1 | score | `deriveSearch` prefilter.ts:118 | prefilter.ts:60-98 | `DerivedSearch` prefilter.ts:100 | minimal (env example: none) | SDK default | `{userId, runId}` | search-run.ts:168 (pg-boss search job) |
| 2 | score | `scoreMatches` fast-score.ts:176 | fast-score.ts:41-61 + candidate block | `FastScore` schemas.ts:26 | minimal | default | `{userId, runId}` | search-run.ts:302; 16 concurrent, batches of 40 |
| 3 | evaluate | `runDeepEval` deep-eval.ts:290 | deep-eval.ts:47-104 + candidate | `DeepEval` schemas.ts:87 | medium | 240000 ms | `{userId, runId}` or `{userId}` | evaluate-run.ts:714 (5 concurrent) |
| 4 | extract | `extractFromText` extract.ts:111 | extract.ts:23-50 | `ExtractedResumeSchema` schema.ts:157 | low | default | `{userId}` | POST /api/uploads (inline) |
| 5 | extract | `extractFromImages` extract.ts:123 (vision, up to 4 PNG pages at scale 2) | extract.ts:52-55 | same | low | default | `{userId}` | same, when garbled |
| 6 | tailor | `tailorResume` tailor.ts:494 | tailor.ts:120-144 + resume + exact_fields | `TailorOutputSchema` tailor.ts:108 | high | default | `{userId, runId}` | tailor-run.ts:109 |
| 7 | verify | `modelJudge` verify.ts:772 | verify.ts:664-680 + original resume | `Verdicts` verify.ts:682 | low | default | `{userId, runId}` | tailor-run.ts:126 |
| 8 | score | bake-off `runScoringFixture` bakeoff-fixture.ts:103 | bakeoff-fixture.ts:27-38 | `FastScore` | override or minimal | default | none, and **not ledgered** | scripts/model-bakeoff.ts |
| 9 | all | llm-smoke llm-smoke.ts:50 | inline | `Answer` | per purpose | default | none | scripts/llm-smoke.ts |
| x1 | extract | careers page `fetchCareersPage` sources/careers-page.ts:125 (call at :153) | careers-page.ts:66-80 | `ExtractedListing` careers-page.ts:45-64 | low | default | caller's context | sources area |
| x2 | chat | `discoverCompanies` tool agents/tools/companies.ts:85 | inline (companies.ts:89-92) | `ProposedCompanies` | low | default | `{userId}` | chat-agent tool (agents area) |
| x3 | chat | ToolLoopAgent / follow-up streams | agents area | n/a | low | n/a | ALS from route | **bypasses the limiter**; ledgered via `wrapStream` |

Common to rows 1-7, 9, x1 and x2: they go through `generateStructured`, so they get the limiter, the reasoning effort, the ALS context, one output retry, and the SDK's 2 transport retries. No call sets temperature, max tokens, provider options or cache control. Caching relies only on putting the stable content first (the instructions), which benefits providers with automatic prefix caching (OpenAI, DeepSeek, Gemini implicit caching). **Anthropic gets no caching**, because no `cache_control` breakpoints are sent.

---

## 23. Invariants to preserve (summary)

1. A purpose resolves to `provider:model` from env, with `LLM_DEFAULT` as the fallback. Never fall back to a different vendor. Fail with a clear message (models.ts:90-110).
2. Every provider call (each transport attempt, failures included) writes one `llm_calls` row with tokens, cost and latency, attributed via context. A ledger write failure never fails the call (ledger.ts:54-86, 89-115).
3. `input_tokens` = uncached + cache-write; `cached_input_tokens` = cache-read; output includes reasoning; reasoning is not charged twice (ledger.ts:35-52, 74; pricing.ts:149-155).
4. Cost is computed and frozen at call time, using the UTC-hour band for DeepSeek (pricing.ts:109-156).
5. Reasoning effort per purpose can be overridden by env at call time (models.ts:142-146).
6. There is exactly one retry on unusable structured output, never on `finishReason === "length"` (structured.ts:50-92).
7. The per-provider rate ceiling is FIFO and defaults to 300 rpm (limiter.ts:28-95).
8. Every model schema has all fields required, with absence as null (OpenAI strict mode).
9. The stable prompt half goes in the instructions and the per-item half in the prompt (fast-score.ts:180-182, deep-eval.ts:136-138, tailor.ts:240-244).
10. Fast scoring:
    - skips already-scored matches
    - archives below the threshold only when the stage is `found`
    - leaves a failed posting unscored and visible

    (fast-score.ts:127-231)
11. A deep-eval `skip` archives the match only from `found` and appends a note (queries/matches.ts:541-548). Citations are filtered to real fact ids, and ids are stripped from prose (deep-eval.ts:218-280).
12. Extraction never invents: the vision fallback applies to PDFs only when garbled, at most 4 pages at scale 2 (extract.ts:88, pdf.ts:31-58).
13. Tailoring keeps only base items, with base order for roles and education, base basics, base skill objects, and base dates when a date is over 40 chars (tailor.ts:331-471).
14. Verification:
    - rules run first
    - the model judges only non-verbatim lines that passed the rules
    - a judge failure reverts every rewrite
    - there is one deterministic revision, then the rules are re-checked
    - the change log is reconciled

    (verify.ts:805-1202)
15. The prefilter is built to keep too much rather than too little:
    - the junior-search seniority guard
    - level words stripped from include terms
    - one role, one match
    - dismissed matches stay out
    - the 45-day age cutoff, skipped for named companies
    - the 600 cap

    (prefilter.ts)
16. A search uses the effective preferences, `applyBrief(saved, brief)`, and the model's `remote: "any"` is discarded unless it came from hints (prefilter.ts:150-155, brief.ts:114-125).

---

## 24. Hazards for a Python/Django port

- **No AI SDK in Python.** You must reproduce, per provider:
  - the reasoning-effort mapping (section 3a)
  - structured-output modes (strict json_schema on the OpenAI Responses API; Anthropic output_format or tool fallback; DeepSeek json_object with the schema injected into the system message)
  - transport retry with Retry-After (section 3c)
  - a total timeout
  - a usage shape with cache read/write and reasoning tokens

  LiteLLM or pydantic-ai covers most of this, but their mappings differ from the AI SDK's: for example DeepSeek `medium→high`, Anthropic adaptive versus budget thinking, and the Gemini 3 `thinkingLevel` minimums. Pin the mapping in a single Python module, the equivalent of models.ts.
- **Middleware-on-model ledger.** The point was to catch agent-loop calls. In Python, put the ledger at the lowest layer every call passes through (a LiteLLM callback or a custom model wrapper), and make sure **each retry attempt** is recorded the same way, or decide deliberately not to.
- **AsyncLocalStorage → contextvars.** Celery threads and `ThreadPoolExecutor` do not inherit context. Copy the context explicitly when fanning out (fast-score uses 16 concurrent calls, deep-eval 5).
- **The limiter is in-process.** With several Celery worker processes or gunicorn workers, a per-process bucket multiplies the real rate. Consider a shared limiter (Redis) or one worker per queue. Keep FIFO fairness and the "starts full" burst semantics if you want the same timing.
- **Zod → Pydantic.** OpenAI strict mode needs `additionalProperties: false` and every key required. Pydantic emits `default`, `title` and `$defs`, so use the OpenAI SDK's strict conversion or a custom generator. Preserve `.describe()` text as `Field(description=...)`. Keep DeepEval's field order, which is deliberate.
- **Validation semantics.** Zod `.int().min(0).max(100)` and `DateString min(1).max(40)` failures trigger the single retry. Matching the latent bug (malformed JSON is not retried) or fixing it is a decision.
- **Exact prompt text fidelity**, for cache hits and behaviour:
  - `JSON.stringify(x)` is compact `{"a":1}` (use `separators=(",", ":")`).
  - `JSON.stringify(x, null, 2)` and `JSON.stringify(x, null, 1)` match `json.dumps(indent=N)`, but you need `ensure_ascii=False`.
  - Dict key order must follow the schema order.
  - JS template interpolation of `null` and `undefined` prints `null`/`undefined`: the fast-score Pay line can print `150000–null`, whereas Python prints `None`.
  - `toLocaleString("en-US")` gives grouping and at most 3 fraction digits, unlike Python `f"{x:,}"` on floats.
  - `toISOString().slice(0, 10)` is a UTC date.
  - Unicode dashes and middle dots appear in prompts and reasons.
- **Regex dialect.** verify.ts and garble.ts use `\p{L}`, `\p{N}` and `\p{Lu}` with the `u` flag, plus lookbehind. Python `re` lacks `\p{..}`, so use the `regex` module. Patterns without the `u` flag use ASCII `\b` and `\w` in JS, so use `re.ASCII` (or the regex module's V0 ASCII flags) for those to match. JS `\s` includes U+FEFF; Python's does not. JS `.length` counts UTF-16 units, so garble ratios and the deep-eval cost estimate differ slightly for astral characters.
- **Number keys** in `extractMetrics`: keys only compare with each other, but the digit path (`fmt` on a float) and the word path (int) must render identically (`n:20`, never `n:20.0`).
- **PDF text extraction.** pdf.js (unpdf) item joining and whitespace normalization (section 17b) differ from pypdfium2, PyMuPDF and pdfminer. The garble thresholds were tuned on pdf.js output; re-validate against `src/test/fixtures/resumes/*.pdf`. Page rendering at scale 2 is a 144 DPI-equivalent PNG.
- **DOCX.** The Python `mammoth` package (same author) has `extract_raw_text`, which is close to identical.
- **Postgres regex** in the prefilter (`~*`, `!~*`, `\y`, `DISTINCT ON`, `regexp_replace` in roleKey, `ilike` on `locations::text`, `location_norm->>'metro'`). The Django ORM needs `RawSQL` or `Func` expressions; keep the patterns byte-identical.
- **Per-call DB writes inside concurrent scoring** (one UPDATE per posting, guarded by `stage = 'found'`). The ORM must use `.filter(...).update(...)` to keep that guard.
- **Inline extraction in a request** (upload route, up to a 10 MB PDF plus a vision call). In Django that blocks a worker thread; consider async views or a job with polling, but note the product decision to keep it inline.
- **Bake-off calls are not ledgered** (they use a raw registry model); decide whether to keep that.
- **The chat agent bypasses the limiter**; decide whether the Python chat path should acquire.
- **Unused or reserved surfaces:** the `merge` purpose, `VISION_PURPOSES` and `extractResume`'s `forceVision`. Keep them in the enum (the DB enum `llm_purpose` includes `merge`).

---

## 25. Python library candidates

| TS dep | Use | Python candidate |
|---|---|---|
| `ai` (generateText, Output.object, wrapLanguageModel, createProviderRegistry, retry) | every LLM call | `litellm` (unified providers, `reasoning_effort`, `response_format`, success and failure callbacks for the ledger, cost hooks), `pydantic-ai` (typed `output_type`, usage incl. cache tokens), or the official `openai`, `anthropic`, `google-genai` SDKs behind one in-house adapter |
| `@ai-sdk/openai`, `anthropic`, `google`, `deepseek`, `openai-compatible` | vendors | the `openai` SDK (Responses API, and DeepSeek and compat via base_url), `anthropic`, `google-genai` |
| `zod` | schemas and validation | `pydantic` v2 (+ `openai.lib._pydantic.to_strict_json_schema` for strict mode) |
| `AsyncLocalStorage` | attribution | `contextvars` |
| `unpdf` (pdf.js) + `@napi-rs/canvas` | PDF text and page PNGs | `pypdfium2` (text + render at scale=2) or `pymupdf` |
| `mammoth` | DOCX raw text | `mammoth` (PyPI) |
| `p-limit`-style `pooled` | bounded concurrency | `asyncio.Semaphore` + `gather` (order preserved) or `concurrent.futures` |
| regex with `\p{}` | verify, garble | `regex` |
| `crypto.randomUUID` | item ids | `uuid.uuid4()` |
