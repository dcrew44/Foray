# LLM library for the Django side of Foray: pydantic-ai 2.54 evaluation

Researched 2026-10-04. Scope: replace `src/lib/llm/*.ts` (AI SDK 7 provider registry, per-purpose
models, per-purpose reasoning effort, structured output, `ToolLoopAgent` with step caps, cost
ledger as model middleware, AsyncLocalStorage attribution, per-provider token bucket) and the chat
agent's streaming to the unchanged `useChat` client.

**Evidence labels.** **[ran]** means a script in `research/llm-library/scripts/` produced the output
quoted here; its `NN_output.txt` sits next to it. **[source]** means I read the installed package's
code. **[docs]** means a web page or release note, not verified by execution.

## Verdict

Use **pydantic-ai 2.54.x**, installed as `pydantic-ai-slim[openai,anthropic,google,ui]`. Pin the
minor version, because it ships several minors a week.

- **Ledger.** Port the cost ledger as a `WrapperModel` subclass, so it sits on the model the same way
  the TS middleware does. **[ran]** It recorded every request with correct cache and reasoning
  counts across `run`, `run_stream`, `run_stream_events`, tool loops, nested agent calls made inside
  tools, vendor errors, approval resumes and a client disconnect mid-stream.
- **Rate limiter.** Port the token bucket as a second `WrapperModel`. Unlike `callLLM` in TS, this
  also gates the calls the agent loop makes.
- **Attribution.** Use `contextvars` in place of AsyncLocalStorage. Under Django, set it **inside**
  the streaming generator, not in the view body.
- **Chat route.** Serve `useChat` with `VercelAIAdapter(sdk_version=7)` driven framework-agnostically.
  **[ran]** The installed `ai@7.0.106` parsed every chunk and rebuilt valid UIMessages, approvals
  included.
- **Reasoning effort.** Keep it as a Foray-owned per-provider mapping. The unified `thinking`
  setting is right for OpenAI, Anthropic and Google, but it is **silently dropped for DeepSeek**, so
  DeepSeek needs `extra_body` plus `openai_reasoning_effort`.

## Versions (installed and exercised)

| Package | Version | Notes |
|---|---|---|
| pydantic-ai / pydantic-ai-slim / pydantic-graph | 2.54.0 | Released 2026-10-03. v2.0.0 stable was 2026-06-23 **[docs]** |
| pydantic | 2.13.5 | |
| openai (Python SDK) | 3.24.0 | Built on **httpx2** **[source]** `openai/_base_client.py: import httpx2` |
| anthropic | 1.11.0 | Built on **httpx2** |
| google-genai | 2.28.0 | Still on legacy **httpx 0.28.1**. pydantic-ai warns that passing an `httpx.AsyncClient` to `GoogleProvider` is deprecated and goes in v3 **[ran]** |
| httpx2 / httpx | 2.13.1 / 0.28.1 | pydantic-ai core requires `httpx2>=2.7`. The `retries` extra still pulls legacy `httpx` |
| genai-prices | 0.1.9 | Cost data used by pydantic-ai |
| tenacity | 9.1.4 | For `pydantic_ai.retries.AsyncHTTPX2TenacityTransport` |
| starlette | 1.7.0 | Only needed for the adapter's `dispatch_request`. Not needed for the Django path |
| Django / asgiref | 6.1.1 / 3.12.1 | Streaming test only |
| litellm (separate venv) | 1.104.0 | Pins **openai 2.54.0** and legacy httpx |
| Python | 3.14.5 (uv-managed) | |
| Node | 26.0.0 | |
| ai (TS, repo) | 7.0.106 | `@ai-sdk/openai` 4.0.70, `anthropic` 4.0.57, `google` 4.0.75, `deepseek` 3.0.48, `openai-compatible` 3.0.52 |

Venvs: `research/llm-library/venv` (pydantic-ai + Django) and `research/llm-library/venv-litellm`.

## 1. What the TS layer does, read from `src/lib/llm/*.ts`

- **`registry.ts`**: `createProviderRegistry` over `openai`, `google`, `anthropic`, `deepseek`, plus
  `compat` (OpenAI-compatible, registered only when `LLM_COMPAT_BASE_URL` is set). Credentials come
  from `OPENAI_API_KEY`, `GOOGLE_GENERATIVE_AI_API_KEY`, `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY` and
  `LLM_COMPAT_API_KEY`.
- **`models.ts`**:
  - `model(purpose)` resolves `LLM_<PURPOSE>` or `LLM_DEFAULT` (a `provider:model` string), fails
    loudly if the vendor's key is missing, and wraps the model in `ledgerMiddleware(purpose, ref)`.
  - `reasoningFor(purpose)` gives an AI SDK portable `reasoning` value
    (`none|minimal|low|medium|high|xhigh`). Defaults: chat low, extract low, score minimal,
    evaluate medium, tailor high, verify low, merge low. `LLM_REASONING_<PURPOSE>` overrides it.
  - `quirksFor` drops `temperature` for Anthropic reasoners.
- **`call.ts`**: `callLLM` does `acquire(provider)`, then `withLlmContext`, then `generateText` with
  the purpose's reasoning.
- **`structured.ts`**: `generateText({ output: Output.object({ schema }) })` with **one** retry on
  schema-invalid output or no output, and no retry on `finishReason=length`.
- **`ledger.ts`**: `wrapGenerate` and `wrapStream` write one `llm_calls` row per model call.
  - `inputTokens` is stored as noCache + cacheWrite. Cached input, reasoning, cost, latency and
    error are stored too.
  - On streams, the row is written in the transform's `flush`, so an aborted stream is still
    recorded.
- **`limiter.ts`**: an in-process token bucket per provider (`LLM_RPM_<PROVIDER>`, default
  300/min). It is only applied in `callLLM`, so the chat agent's loop is not rate-limited today.
- **`context.ts`**: AsyncLocalStorage holding `{ userId, runId }`.
- **Agent** (`agents/chat-agent.ts`): `ToolLoopAgent` with `stopWhen: isStepCount(8)` (4 for
  follow-ups) and `activeTools` for follow-ups.
  - **The chat agent uses no AI SDK `toolApproval` today.** Approvals are the app's DB continuation
    pattern (`answer_approval` tool, `lib/runs/approvals`). SDK-level approval is therefore an
    option, not a migration requirement.
- **Chat route**: `createAgentUIStreamResponse` with `generateMessageId: generateId`. The comment
  warns that an empty message id collides on the `messages` primary key. History is stored as AI
  SDK 7 UIMessages in Postgres.
  - The client (`components/chat/chat.tsx`) sends only `{ id, message: all.at(-1) }`.
- **Vision**: `resume/extract.ts` sends PNG page images as `file` parts.

## 2. Provider coverage and how `provider:model` maps **[ran]** `01_infer_model.py`

```
openai:gpt-5.6-luna        -> OpenAIResponsesModel  system='openai'    base_url=https://api.openai.com/v1/
google:gemini-3.8-flash    -> GoogleModel           system='google'    base_url=https://generativelanguage.googleapis.com/
anthropic:claude-haiku-4-5 -> AnthropicModel        system='anthropic' base_url=https://api.anthropic.com
deepseek:deepseek-flash    -> OpenAIChatModel       system='deepseek'  provider=DeepSeekProvider base_url=https://api.deepseek.com
openai-chat:gpt-5.6-luna   -> OpenAIChatModel       (Chat Completions, if ever needed)
compat:foo                 -> UserError Unknown model: compat:foo
bare 'gpt-5'               -> UserError Unknown model: gpt-5. Did you mean 'openai-chat:gpt-5'?
```

- **Same strings as today.** `infer_model("provider:model")` accepts Foray's existing env values
  unchanged. Since v2, `openai:` means the **Responses API** **[docs]** (changelog v2.0.0b1). AI SDK
  `@ai-sdk/openai` also defaults to Responses, so behaviour matches.
- **`compat` is not a pydantic-ai prefix.** Build it explicitly as
  `OpenAIChatModel(name, provider=OpenAIProvider(base_url=LLM_COMPAT_BASE_URL, api_key=LLM_COMPAT_API_KEY))`
  **[ran]**. Alternatively, pass a custom `provider_factory` to
  `infer_model(model, provider_factory=...)` **[source]** `models/__init__.py:1746`.
- **Env var mismatch (Google).** `GoogleProvider` reads `GOOGLE_API_KEY` (or legacy `GEMINI_API_KEY`),
  **not** `GOOGLE_GENERATIVE_AI_API_KEY` **[source]** `providers/google.py:149`. DeepSeek reads
  `DEEPSEEK_API_KEY`. While TS and Python share one `.env`, build providers in Foray's own registry
  with `api_key=` taken from Django settings. Do not rely on pydantic-ai's env lookup.
- **Vision.** pydantic-ai takes images as `BinaryContent(data, media_type='image/png')` in the user
  prompt. Not exercised here, but it is a first-class `UserContent` type **[source]** `__init__.py` exports.

## 3. Reasoning effort: what reaches the wire **[ran]** `02_providers_wire.py`

The real model classes were run against mocked httpx2/httpx transports, and the request JSON was
captured. pydantic-ai has a unified `ModelSettings['thinking']: bool | 'minimal'|'low'|'medium'|'high'|'xhigh'`.
Provider-specific keys take precedence over it **[source]** `settings.py:437`.

| Foray effort | OpenAI Responses `gpt-5.6-luna` | Anthropic `claude-haiku-4-5` (budget model) | Anthropic `claude-sonnet-5` (adaptive) | Google `gemini-3.8-flash` | DeepSeek `deepseek-flash` |
|---|---|---|---|---|---|
| unset | `reasoning:{context:all_turns}` | nothing | nothing | no thinkingConfig | nothing |
| none → `thinking=False` | `effort:"none"` | **nothing sent** | `thinking:{type:disabled}` | `thinking_level: LOW` (model minimum) | **nothing sent** |
| minimal | `effort:"low"` (GPT-5.6 has no minimal) | `enabled, budget 1024` | `adaptive` + `output_config.effort: low` | `LOW` + `include_thoughts:true` | **nothing sent** |
| low | `low` | budget 2048 | adaptive/low | LOW | **nothing sent** |
| medium | `medium` | budget 10000 | adaptive/medium | MEDIUM | **nothing sent** |
| high | `high` | budget 16384 | adaptive/high | HIGH | **nothing sent** |
| xhigh | `xhigh` | budget 32768 | adaptive/xhigh | HIGH | **nothing sent** |

### Where it differs from AI SDK 7.0.106

These come from reading `node_modules/@ai-sdk/*/dist/index.js` and
`@ai-sdk/provider-utils` `mapReasoningToProviderBudget`.

- **DeepSeek is the critical gap.**
  - The `DeepSeekProvider` profile reports `supports_thinking = False` and
    `thinking_enabled_by_default = False` **[ran]** `07_quirks.py`, so the unified `thinking` field
    is ignored for every value.
  - AI SDK sends `thinking:{type:"disabled"}` for `none`. It sends
    `thinking:{type:"enabled"}, reasoning_effort` with the map minimal→low, low→low, medium→high,
    high→high, xhigh→max.
  - AI SDK also treats V4 models as thinking-on by default (`isDeepSeekV4Model`).
  - So a naive port of `LLM_REASONING_SCORE=none` would leave DeepSeek Flash reasoning at its
    default. That is the "92% of output tokens are reasoning" cost case `models.ts` calls the
    largest cost lever.
  - **Fix, verified on the wire:**
    - `none` → `{'extra_body': {'thinking': {'type': 'disabled'}}}` produced
      `"thinking": {"type": "disabled"}`.
    - Other levels → `{'extra_body': {'thinking': {'type': 'enabled'}}, 'openai_reasoning_effort': <mapped>}`
      produced `"reasoning_effort": "max", "thinking": {"type": "enabled"}`.
- **OpenAI `minimal`.**
  - pydantic-ai maps it to `low` for GPT-5.6, on purpose **[docs]** (thinking page: "GPT-5.6 maps
    unified 'minimal' to 'low'").
  - AI SDK passes `minimal` through.
  - `openai_reasoning_effort='minimal'` forces the raw value **[ran]**, if parity is ever wanted.
- **Anthropic budget models.**
  - pydantic-ai uses fixed budgets (1024/2048/10000/16384/32768).
  - AI SDK uses a percentage of the model's max output (2/10/30/60/90% of 64000, so
    1280/6400/19200/38400/57600, minimum 1024).
  - pydantic-ai is cheaper per call. Re-run `pnpm llm:bakeoff`-style fixtures to confirm quality on
    `tailor`/`evaluate` if Haiku 4.5 is used there.
  - `thinking=False` on Haiku 4.5 sends nothing; AI SDK sends `disabled`. This is equivalent because
    Haiku's default is off.
- **Google.** The level mapping is identical to AI SDK, including `none` becoming the model's
  minimum level. pydantic-ai additionally sets `include_thoughts: true`, so thought summaries come
  back as `ThinkingPart`s.
- **Anthropic `max_tokens`.** It defaults to the model maximum (64000 for Haiku 4.5, 128000 for
  Sonnet 5). Long requests are then streamed internally even for non-streaming calls
  **[source]** `models/anthropic.py:1340`. Billing is unaffected.
- **Anthropic + temperature.**
  - For `claude-sonnet-5`, pydantic-ai drops `temperature` with the warning "Sampling parameters
    ['temperature'] are not supported" **[ran]**.
  - For `claude-haiku-4-5` with thinking it **still sends `temperature: 0.2`** **[ran]**.
  - The live API rejects non-default temperature with extended thinking. That is how `models.ts`
    describes it; it was not re-verified live. Keep Foray's `quirks_for(ref)` to drop temperature
    for Haiku 4.5 when thinking is on.

### Recommended Python `reasoning_settings(ref, effort) -> ModelSettings`

- `openai`, `anthropic`, `google`: `{'thinking': False if effort == 'none' else effort}`.
- `deepseek`: the `extra_body` + `openai_reasoning_effort` mapping above, mirroring AI SDK's table.
- `compat`: `{}`, or `openai_reasoning_effort` behind a flag.

Golden-test the request bodies with the mock-transport technique from `02_providers_wire.py`.

**Carrying settings on the model** **[ran]** `10_settings_on_model.py`:

- A `WrapperModel` whose `settings` property returns the purpose settings is honoured by the
  **Agent** path. Agent-level and run-level `model_settings` merge over it.
- `pydantic_ai.direct.model_request` ignores `Model.settings`; it saw `{}`.
- So the Foray wrapper should also merge purpose settings in `request()`/`request_stream()` as
  `{**purpose, **(model_settings or {})}`.

## 4. Usage fields per provider **[ran]** `02_providers_wire.py`, canned vendor JSON/SSE

`RequestUsage` fields are `input_tokens`, `cache_read_tokens`, `cache_write_tokens`,
`output_tokens`, `details: dict[str,int]` and `cost: Decimal|None`.

- **`input_tokens` includes cache reads and writes for every vendor.** It is normalised; Anthropic's
  raw `input_tokens` is re-summed **[source]** `usage.py` docstring.
- **`output_tokens` includes reasoning tokens for every vendor here.** Gemini's
  `candidatesTokenCount` 100 plus `thoughtsTokenCount` 200 became `output=300` **[ran]**.

| Vendor (canned raw usage) | input | cache_read | cache_write | output | reasoning lives in |
|---|---|---|---|---|---|
| OpenAI Responses: input 1000, cached 600, output 300, reasoning 200 | 1000 | 600 | 0 | 300 | `details['reasoning_tokens']`, also extra attr `output_reasoning_tokens` |
| Anthropic: input 100, cache_creation 500, cache_read 400, output 300, thinking 200 | **1000** | 400 | 500 | 300 | `details['thinking_tokens']`; **no** `output_reasoning_tokens` |
| Google: prompt 1000, cached 600, candidates 100, thoughts 200 | 1000 | 600 | 0 | **300** | `details['thoughts_tokens']`, also `output_reasoning_tokens` |
| DeepSeek (chat): prompt 1000, cache_hit 600, completion 300, reasoning 200 | 1000 | 600 | 0 | 300 | `details['reasoning_tokens']`, also `output_reasoning_tokens`; raw `prompt_cache_hit_tokens` kept in details |

**Ledger mapping** (same column semantics as `ledger.ts`):

```python
REASONING_KEYS = ("reasoning_tokens", "thinking_tokens", "thoughts_tokens")  # openai+deepseek / anthropic / google
uncached = max(u.input_tokens - u.cache_read_tokens - u.cache_write_tokens, 0)   # TokenCounts.inputTokens
row.input_tokens        = uncached + u.cache_write_tokens                        # == u.input_tokens - u.cache_read_tokens
row.cached_input_tokens = u.cache_read_tokens
row.output_tokens       = u.output_tokens
row.reasoning_tokens    = next((u.details[k] for k in REASONING_KEYS if u.details.get(k)), 0)
cost = costUsd(ref, {input: uncached, cached: cache_read, cacheWrite: cache_write, output, reasoning})  # Foray's pricing.py
```

- **`usage.cost` is `None` at the model layer.** It is filled by the agent graph
  (`_genai_prices.fill_response_cost`) **[source]**. It was `Decimal('0.000216')` on `RunUsage`
  **[ran]** `04`.
- **genai-prices 0.1.9 knows** `claude-haiku-4-5`, `gpt-5.6-luna`, `gemini-3.8-flash` and
  `deepseek-chat`. It does **not** know `deepseek-flash` (LookupError) **[ran]**, and it has no
  DeepSeek off-peak bands.
- **Keep Foray's `pricing.ts` table**, ported, as the ledger's cost source. Optionally store
  genai-prices' figure alongside as a cross-check.

## 5. The ledger on the model **[ran]** `03_ledger_wrapper.py`, `04_real_stream_ledger.py`; draft in `scripts/ledger_lib.py`

### Seam options

| Option | Sees agent-internal calls | Sees streams | Sees `direct.model_request` / any Agent without extra wiring | Verdict |
|---|---|---|---|---|
| `WrapperModel` subclass overriding `request` and `request_stream` (`@asynccontextmanager`) | yes | yes; usage read in `finally` after the stream ends | yes, because it is on the model | **Use this.** It mirrors `wrapLanguageModel` |
| Capability `wrap_model_request` (Agent-level hook) | yes | yes; ran: streamed usage 4 and 5 seen | **no**: only Agents that list the capability | Good for per-agent policy, not for a ledger |
| `InstrumentedModel` / OpenTelemetry / Logfire | yes | yes | yes | Observability, not a DB ledger. Could feed one via a span processor, but that is indirect |

`InstrumentedModel` is itself a `WrapperModel` that records after the stream in a `finally` block
**[source]** `models/instrumented.py:373`. The draft follows the same pattern.

### Draft (core of `scripts/ledger_lib.py`, executed)

```python
class LedgerModel(WrapperModel):
    async def request(self, messages, model_settings, model_request_parameters):
        started = time.perf_counter()
        try:
            response = await self.wrapped.request(messages, model_settings, model_request_parameters)
        except BaseException as e:
            await self._write(RequestUsage(), started, mode="request", error=e); raise
        await self._write(response.usage, started, mode="request", state=response.state)
        return response

    @asynccontextmanager
    async def request_stream(self, messages, model_settings, model_request_parameters, run_context=None):
        started = time.perf_counter(); stream = None; error = None
        try:
            async with self.wrapped.request_stream(messages, model_settings, model_request_parameters, run_context) as stream:
                yield stream
        except BaseException as e:
            error = e; raise
        finally:
            usage = stream.usage if stream is not None else RequestUsage()   # property in v2, final once drained
            state = stream.get().state if stream is not None else None       # complete | incomplete | interrupted
            await asyncio.shield(self._write(usage, started, mode="stream", error=error, state=state))
```

### Results

| Scenario | Ledger rows | Notes |
|---|---|---|
| 1 `agent.run` + 1 tool call | 2 rows (`request`) | Per-row `cached_input_tokens` 600 and 1000, `reasoning` 30 and 5. Sum equals `RunUsage(input=2200, cache_read=1600, cache_write=100, output=70)` |
| 2 `agent.run_stream` + tool | 2 rows (`stream`) | Ledger output sum 9 equals `run.output_tokens` 9 |
| 3 `run_stream_events` (what the Vercel adapter drives) | 2 rows | In v2 `run_stream_events()` is an async **context manager**, not an iterator |
| 5 vendor raises | 1 row | `error='RuntimeError: HTTP 529 overloaded'`, zero tokens |
| 6 three concurrent `asyncio.gather` runs, each tool making a nested `Agent.run` on an `extract` model | 12 rows | Each row has the right `user_id`/`run_id`: `{'user0': ['chat','extract','extract','chat'], …}` |
| 7 runaway loop, `UsageLimits(request_limit=4)` | 4 rows | Then `UsageLimitExceeded` |
| 8 structured output (ToolOutput, NativeOutput, PromptedOutput) | 1 row each | |
| 9 approval request, then approve in a new request, then deny | 1 row per model call | |
| 04-A real `OpenAIResponsesModel`, SSE stream + tool call | 2 rows | `cached_input_tokens` 600 and 1000, `reasoning_tokens` 25 and 10 from `response.completed`; wire `stream: true`, `reasoning: {effort: low}` |
| 04-C client disconnect: task cancelled 0.3s into a slow stream | 1 row | `state='incomplete'`, `error='CancelledError: …'`, partial output counted. `asyncio.shield` lets the write finish while the task is cancelling |

Scenario 4 in `03` (breaking out of `stream_text` early) recorded complete rows only because
FunctionModel had already produced everything. 04-C is the real abort test.

### Retry visibility differs from TS

The vendor SDKs retry internally (`DEFAULT_MAX_RETRIES = 2` in both `openai/_constants.py` and
`anthropic/_constants.py` **[source]**), **below** the WrapperModel. Failed attempts that the SDK
retried therefore do not produce ledger rows. In AI SDK the retry loop sits above the middleware,
so each attempt is a row; that is inferred from AI SDK's design and not executed here. A 429
attempt is not billed, so the ledger impact is cosmetic.

## 6. Attribution: contextvars in place of AsyncLocalStorage **[ran]** `03`, `09_django_stream.py`

- **The swap works.** A `ContextVar[LlmContext]` set with a `with with_llm_context(user_id=…, run_id=…)`
  block propagates into agent-internal model calls, sync tools (run in threads), async tools and
  nested agents. pydantic-ai copies the context into worker threads
  (`_utils.run_in_executor` uses `copy_context()` / anyio `run_sync`) **[source]**.
- **Concurrent runs stay isolated** (scenario 6).
- **`run_stream_events` keeps attribution.** It starts a background task, which copies the context
  at first `__anext__` **[source]** `agent/abstract.py:143`; scenario 3 attributed correctly.
- **Django 6.1 ASGI pitfall.** `StreamingHttpResponse` iterates the body **after the view returns**.

  | Where the context is set | Ledger `user_id` |
  |---|---|
  | `with` block in the view body | **`None`**: the block had exited |
  | `ContextVar.set` in the view, never reset | Attributed, but leaks; do not rely on it |
  | Inside the async generator passed to `StreamingHttpResponse` | **Correct** |

  So the chat view must enter `with_llm_context` **inside the body generator**. Resolve the user
  from the session before returning.
- **Worker jobs.** Wrap the job handler body.

## 7. Agent loop, step caps, retries

- **`ToolLoopAgent({stopWhen: isStepCount(8)})` becomes `UsageLimits(request_limit=8)`.**
  - Difference: AI SDK stops **gracefully** after N steps and returns what it has. pydantic-ai
    **raises `UsageLimitExceeded`** **[ran]** scenario 7. In the chat stream that would surface as
    an error chunk.
  - For parity, add a capability whose `prepare_tools` returns `[]` once
    `ctx.usage.requests >= cap - 1`, which forces a final text turn. Keep `request_limit` as the
    hard backstop. **[ran]** `11_graceful_step_cap.py`: a tool-greedy model with `StepCap(4)` plus
    `UsageLimits(request_limit=4)` advertised tools per step as `[1, 1, 1, 0]` and ended normally
    with text after 4 requests, without raising.
    - This is better than AI SDK's hard stop, which can end on a tool result with no text.
    - Cost: the model sees the tool list vanish on the last step.
  - When no limits are passed, pydantic-ai applies `UsageLimits()` with `request_limit=50`
    **[source]** `agent/__init__.py:1707` and `usage.py`. (The docs page summary claimed "unlimited";
    the source wins.)
- **`activeTools` for follow-ups.** Use a capability `prepare_tools` filter or a `FilteredToolset`
  (exported) to expose only `FOLLOW_UP_TOOLS`.
- **Structured-output retry.**
  - The default output budget is 1 retry, so 2 requests at most. That matches `structured.ts`'s
    one-retry policy **[ran]** `07`: `ToolOutput` gave 2 model requests, then
    `Score(score=4,…)`; always-invalid `NativeOutput` gave
    `UnexpectedModelBehavior: Exceeded maximum output retries (1)` after 2 requests.
  - Semantics differ: pydantic-ai continues the conversation with a `retry-prompt` part carrying
    the Pydantic validation errors, where TS re-sent the original request.
  - It is tunable with `Agent(retries={'output': n, 'tools': m})` (`AgentRetries`).
  - The TS rule "no retry on `finishReason=length`" was not verified here.
- **Output modes.**
  - The default `ToolOutput` works on all providers.
  - `NativeOutput` needs json_schema support. The DeepSeek profile lacks it for chat; it has
    `supports_json_object_output=True` and `supports_forced_tool_choice_with_thinking=True`.
  - `PromptedOutput` is the analogue of AI SDK's schema-in-system-prompt fallback.
  - Recommendation: default `ToolOutput`, with `PromptedOutput` only if DeepSeek misbehaves.
- **HTTP retries.**
  - The vendor SDK `max_retries` defaults to 2, which matches AI SDK's `maxRetries=2`.
  - For Retry-After-aware policies, `pydantic_ai.retries.AsyncHTTPX2TenacityTransport` plus
    `wait_retry_after` wraps an httpx2 transport **[source]** `retries.py:239`.
  - `GoogleProvider(retry_options=HttpRetryOptions(...))` covers Gemini.
  - `FallbackModel` exists if a vendor fallback is ever wanted. Foray deliberately has none.

## 8. Rate limiter **[ran]** `04` part B

- **pydantic-ai has no rate limiter.** `ConcurrencyLimiter` / `ConcurrencyLimitedModel` is an
  anyio semaphore for concurrency, not requests per minute **[source]** `concurrency.py`.
- **Ported bucket.** `TokenBucket` in `ledger_lib.py` is a lock-free reservation bucket: it
  reserves synchronously between awaits, then sleeps until its slot. It is FIFO and loop-agnostic,
  with no `asyncio.Lock` bound to one loop.
- **Applied as `RateLimitedModel(WrapperModel)`.** It now gates agent-loop calls too. Run A took
  2 tokens for 2 streamed agent requests. Run B: 3 calls through a 60 rpm bucket holding 1 token
  took **2.00s**, as predicted.
- **Caveats:**
  - A cancelled waiter keeps its reservation, which over-throttles slightly.
  - Buckets are per process, exactly as in TS. With Django, the Python worker and the TS worker all
    running during the strangler, effective RPM is the sum. If that matters, implement the same
    interface against Postgres. `AbstractConcurrencyLimiter` shows the intended extension shape.

## 9. Tool approval and the HTTP round trip **[ran]** `03` scenario 9, `05_vercel_adapter.py`, `06_validate_with_ai_sdk.mjs`

### Core API

- Mark a tool with `@agent.tool_plain(requires_approval=True)` (or raise `ApprovalRequired`, or use
  `ApprovalRequiredToolset`).
- With `output_type=[str, DeferredToolRequests]`, the run ends with `DeferredToolRequests(approvals=[ToolCallPart…])`.
- Persist `ModelMessagesTypeAdapter.dump_json(result.all_messages())`.
- On the next HTTP request, call
  `agent.run(message_history=…, deferred_tool_results=DeferredToolResults(approvals={id: True | ToolDenied('…') | ToolApproved(override_args=…)}))`.
- Ran: the tool ran exactly once (`effects == ['staff eng']`). Denial ran nothing.
- Replaying an answered approval raised
  `UserError: Tool call results were provided, but the message history does not contain any unprocessed tool calls`.
- Inline resolution is also possible via the `handle_deferred_tool_calls` capability hook.

### Through `useChat` (AI SDK 7 wire)

`VercelAIAdapter(agent=…, run_input=SubmitMessage(id, messages=[…]), accept='text/event-stream', sdk_version=7, server_message_id=…)`,
then `adapter.encode_stream(adapter.run_stream(on_complete=…))` yields `str` frames that Django's
`StreamingHttpResponse` can stream. No Starlette is needed: `dispatch_request` is the only
Starlette-bound method.

- **Turn 1** emitted `start, start-step, tool-input-start, tool-input-delta, tool-input-available, message-metadata, tool-approval-request, finish-step, finish, [DONE]`.
- **Turn 2.** The client returned the part with `state: 'approval-responded', approval: {id, approved: true}`.
  `adapter.deferred_tool_results` became `DeferredToolResults(approvals={'call_search_1': True})`.
  The stream was then `tool-input-available, tool-output-available, start-step, text-*, …, finish`.
- **Node check against the installed `ai@7.0.106`**: `parseJsonEventStream({schema: uiMessageChunkSchema})`
  accepted all 9 and 14 chunks, rejecting 0. `readUIMessageStream` rebuilt the assistant message
  with the tool part in `approval-requested` and then `output-available`.
  `safeValidateUIMessages(...)` returned `success=true` on the persisted messages.
- **TS-persisted history loads.** A TS-shaped v7 UIMessage history
  (`tool-get_profile_summary`, `output-available`, `step-start`) loads with
  `VercelAIAdapter.load_messages` into request → tool-call → tool-return → text **[ran]**.

### Gaps to handle in the Django view (all ran)

1. **`SubmitMessage.messages` is required**, but Foray's client sends only `message`. The view
   builds `run_input` from Postgres history plus the incoming message. This is the TS route's
   trust model too.
2. **Without `server_message_id` the `start` chunk has no `messageId`, and the client's message id
   is `""`.** This is exactly the `messages` primary-key collision the TS route comments on. With
   `server_message_id` the rebuilt id was `srv-turn1` **[ran]**.
3. **`dump_messages` reshapes history.**
   - The TS message `m2` (one assistant UIMessage containing a tool step and a text step) came back
     as **two** assistant UIMessages.
   - `step-start` parts were dropped.
   - Assistant ids were regenerated (`01a108b4-…-4`) instead of matching the streamed
     `server_message_id`. A `generate_message_id` callback exists.
   - The docstring lists further lossy cases: retry prompts, invalid-JSON args.
   - **Recommendation:** keep a server-side `ModelMessagesTypeAdapter` JSON history as the
     authority for the agent. For the UI `messages` table, persist one assistant UIMessage per turn
     under `server_message_id`, merging consecutive assistant UIMessages from
     `dump_messages(result.new_messages(), generate_message_id=…)`. Golden-test it against the TS
     output.
4. **Response headers.**
   - The adapter sets only `content-type: text/event-stream` and `x-vercel-ai-ui-message-stream: v1`.
   - AI SDK's `UI_MESSAGE_STREAM_HEADERS` also send `cache-control: no-cache`,
     `connection: keep-alive` and `x-accel-buffering: no`. Add them in the view; `09` does this.
5. **Message metadata.**
   - The adapter emits `message-metadata` `{pydantic_ai:{timestamp}}`, which lands in
     `UIMessage.metadata`.
   - Foray's client uses metadata `kind` (`follow_up`, `run_update`). Check that the client
     tolerates the extra key, or strip it in a stream transform.
6. **The approval answer arrives as an assistant message.** With the single-message transport the
   client sends the *assistant* message back as `message`. The view must replace the stored
   assistant message with the same id rather than append it.

## 10. Prompt caching (Anthropic) **[ran]** `07_quirks.py`

| Setting | Where `cache_control` lands |
|---|---|
| `anthropic_cache_instructions=True` | `system[0]` |
| `anthropic_cache_tool_definitions='1h'` | `tools[0]`, the last tool |
| `anthropic_cache_messages=True` | The last message's content, `{'type':'ephemeral','ttl':'5m'}` |
| `anthropic_cache=True` | Top-level `cache_control`, Anthropic automatic caching |

Cache writes and reads come back as `cache_write_tokens` / `cache_read_tokens` (section 4). The TS
code sets no explicit Anthropic cache control today, so this is optional upside.

## 11. LiteLLM and direct vendor SDKs (brief)

### LiteLLM 1.104.0 **[ran]** `08_litellm.py`, separate venv

- **Strengths:**
  - One OpenAI-shaped `acompletion` across vendors.
  - `reasoning_effort` is translated per vendor:
    - Anthropic: `thinking budget 1024`
    - Gemini: `thinkingConfig.thinkingLevel: low` with `includeThoughts`
    - **DeepSeek: `thinking:{type:enabled}`**, which it handles where pydantic-ai does not
    - OpenAI: `reasoning_effort`
  - A `CustomLogger.async_log_success_event` callback received usage and `response_cost`.
- **Weaknesses for Foray:**
  - No agent loop, tool execution, approval, typed structured output with validation retries, or
    `useChat` adapter. All of that would be hand-written.
  - Usage arrives in OpenAI Chat shape (`prompt_tokens_details.cached_tokens`,
    `completion_tokens_details.reasoning_tokens`), plus Anthropic-specific keys.
  - The callback is a global registry, not a per-model seam.
  - It pins `openai==2.54.0` and legacy `httpx`, so it cannot share pydantic-ai's openai 3.x /
    httpx2 stack cleanly in one venv.
  - Supply-chain history: PyPI releases 1.82.7 and 1.82.8 were compromised on 2026-03-24 (TeamPCP,
    a `.pth` credential stealer) **[docs]**. LiteLLM sits next to every provider key.
- **Verdict:** not as the primary layer. It remains an option as an OpenAI-compatible gateway
  behind the `compat` slot if wanted.

### Direct vendor SDKs (`openai` 3.24, `anthropic` 1.11, `google-genai` 2.28)

- These are what pydantic-ai uses underneath.
- Going direct means rewriting four request and stream normalisers, the tool loop, output
  validation and retry, the approval state machine, and the AI SDK UI wire encoder.
- That is roughly the 885-line `src/lib/llm` plus everything AI SDK did for it.
- Only worth it for one-off calls that need a feature pydantic-ai has not exposed. pydantic-ai lets
  you pass `openai_client=` or `client=` to its providers, so the escape hatch exists without
  leaving the framework.

## 12. Other operational notes **[ran]**

- **Banner.** pydantic-ai 2.x prints an ASCII banner, "observability: off…", on the first agent run.
  Set `PYDANTIC_AI_NO_BANNER=1` in Django and worker environments.
- **Release cadence and pinning.** 2.45–2.54 shipped 2026-09-18 to 2026-10-03. The policy is no
  intentional breaking changes in minors; deprecations last until the next major, at least 3 months
  apart; v1 gets security fixes for 6 months after 2026-06-23 **[docs]**. Pin `~=2.54.0` and bump
  deliberately with the golden tests above.
- **The package bundles agent skills**, including
  `pydantic_ai/.agents/skills/migrating-vercel-ai-sdk-and-eve-to-pydantic-ai/SKILL.md`. Its guidance
  matches what was found here: configure `VercelAIAdapter` for the installed SDK version,
  golden-test the wire, step-cap semantics differ, and history round trips are lossy.

## Scripts (all under `research/llm-library/scripts/`)

| Script | Proves |
|---|---|
| `01_infer_model.py` | `provider:model` → model class and provider mapping |
| `02_providers_wire.py` + `02_output.txt` | Reasoning settings on the wire and `RequestUsage` normalisation for OpenAI, Anthropic (Haiku 4.5 and Sonnet 5), Google and DeepSeek |
| `ledger_lib.py` | **Draft port**: `LlmContext`/`with_llm_context`, `tokens_from_usage`, `LedgerModel`, `TokenBucket`, `RateLimitedModel` |
| `03_ledger_wrapper.py` + `03_output.txt` | Ledger over run, stream, events, errors, concurrency, nesting, UsageLimits, output modes, approvals, capability comparison (FunctionModel) |
| `04_real_stream_ledger.py` + `04_output.txt` | Ledger + bucket on the real `OpenAIResponsesModel` over mocked SSE; disconnect mid-stream |
| `05_vercel_adapter.py` + `out/*.sse`, `out/*.json` | `VercelAIAdapter(sdk_version=7)` approval round trip without Starlette |
| `06_validate_with_ai_sdk.mjs` + `06_output*.txt` | The installed `ai@7.0.106` parses the Python stream and validates persisted UIMessages |
| `07_quirks.py` + `07_output.txt` | Anthropic cache placement, temperature + thinking, DeepSeek profile, output retries |
| `08_litellm.py` + `08_output.txt` | LiteLLM usage shape, reasoning mapping, callback |
| `09_django_stream.py` + `09_output.txt` | Django 6.1 ASGI streaming and where to set the contextvar |
| `10_settings_on_model.py` + `10_output.txt` | Purpose settings carried on the model: Agent honours them, `direct` does not |
| `11_graceful_step_cap.py` + `11_output.txt` | `isStepCount`-style graceful stop via a `prepare_tools` capability |

## Sources

- PyPI pydantic-ai release history (2.54.0 on 2026-10-03): https://pypi.org/project/pydantic-ai/
- Upgrade guide and changelog (v2 breaking changes: `openai:` means Responses, bare names error,
  capabilities, `DeferredToolRequests`): https://pydantic.dev/docs/ai/project/changelog/
- Version policy: https://pydantic.dev/docs/ai/project/version-policy/
- Agent / usage limits: https://pydantic.dev/docs/ai/core-concepts/agent/
- Thinking (GPT-5.6 minimal→low; DeepSeek documented only for Bedrock): https://pydantic.dev/docs/ai/capabilities/thinking/
- LiteLLM compromise: https://www.netspi.com/blog/executive-blog/ai-ml-pentesting/litellm-supply-chain-compromise/ ,
  https://www.comet.com/site/blog/litellm-supply-chain-attack/
- Installed sources read: `venv/lib/python3.14/site-packages/pydantic_ai/{usage.py, settings.py, models/{__init__,wrapper,instrumented,function,openai,anthropic,google}.py, providers/{google,deepseek,openai}.py, capabilities/abstract.py, _deferred.py, concurrency.py, retries.py, _utils.py, ui/_adapter.py, ui/vercel_ai/*}`,
  `openai/_constants.py`, `anthropic/_constants.py`; the repo's `node_modules/ai/dist/index.js`
  (`uiMessageChunkSchema`, `UI_MESSAGE_STREAM_HEADERS`) and
  `node_modules/@ai-sdk/{anthropic,google,deepseek,openai}/dist/index.js` (reasoning maps).

## Verification (adversarial pass, 2026-10-04)

Four claims were checked against freshly installed packages in a separate venv,
`research/llm-library-verify/venv`: pydantic-ai-slim 2.54.0, openai 3.24.0, anthropic 1.11.0,
google-genai 2.28.0, httpx2 2.13.1, httpx 0.28.1, Python 3.14.5. Throwaway venvs `venv-pai-1.107.7`
and `venv-pai-2.0.0` were added for the version comparison.

Scripts and outputs are in `research/llm-library-verify/scripts/`. **[ran]** means a script there
produced the result; **[source]** and **[docs]** mean the same as above. No claim was refuted, but
two carry corrections that matter for the port.

### Claim 1: GoogleProvider ignores `GOOGLE_GENERATIVE_AI_API_KEY`

**Confirmed [ran] + [source].**

- `pydantic_ai/providers/google.py:149` reads
  `api_key = api_key or os.getenv('GOOGLE_API_KEY') or os.getenv('GEMINI_API_KEY')`.
- `v1_google_env.py` runs each env combination in a fresh subprocess, against a mock httpx2
  transport that captures the outgoing `x-goog-api-key` header:

| Env | `infer_model('google:gemini-3.8-flash')` | Key sent on the wire |
|---|---|---|
| only `GOOGLE_GENERATIVE_AI_API_KEY` | `UserError: Set the GOOGLE_API_KEY …` | none, raises |
| only `GOOGLE_API_KEY` | GoogleModel | `k-google` |
| only `GEMINI_API_KEY` | GoogleModel | `k-gemini` |
| both | GoogleModel | `k-google` (takes precedence) |
| TS var + `GoogleProvider(api_key=…)` | string inference still raises | `k-explicit` |

- The repo uses `GOOGLE_GENERATIVE_AI_API_KEY` (`src/env.ts:42`, `src/lib/llm/registry.ts:56`,
  `.env.example:24`), so the recommendation stands: pass `api_key=` from Django settings.
- `GoogleCloudProvider` does the same env lookup (`providers/google_cloud.py:88`).

### Claim 2: vendor SDK retries sit below the WrapperModel

**Confirmed for OpenAI and Anthropic [ran]. The Google clause needs a correction.**

**OpenAI and Anthropic.**

- `openai/_base_client.py:37` and `anthropic/_base_client.py:39` do `import httpx2`. Both
  `_constants.py` files set `DEFAULT_MAX_RETRIES = 2`. Their METADATA requires `httpx2<3`.
- pydantic-ai never sets `max_retries` [source]: `providers/_openai_compatible.py` builds
  `AsyncOpenAI(base_url, api_key, http_client, default_headers)`, and `providers/anthropic.py:179/184`
  builds `AsyncAnthropic(api_key, base_url, http_client)`.
- At runtime `provider.client.max_retries == 2` for both.

`v2_sdk_retries.py` runs the real model classes through a counting `WrapperModel`, using a flaky
httpx2 mock transport:

```
openai: 2x429 then 200               http_attempts=3 wrapper_rows=['ok']
openai: 3x429 (exhaust retries)      http_attempts=3 wrapper_rows=['error:ModelHTTPError']
openai: 2x500 then 200               http_attempts=3 wrapper_rows=['ok']
anthropic: 2x429 then 200            http_attempts=3 wrapper_rows=['ok']
anthropic: 2x529 then 200, run_stream http_attempts=3 wrapper_rows=['ok(stream)']
anthropic: 3x429 (exhaust retries)   http_attempts=3 wrapper_rows=['error:ModelHTTPError']
```

**The TS comparison, now executed.** `v2b_ai_sdk_retry_vs_middleware.mjs` ran against the repo's
`ai@7.0.106`, using `wrapLanguageModel` and a `MockLanguageModelV4` that throws a retryable 429
twice:

```
{"attempts":3,"middlewareRows":["error:429","error:429","ok"]}
```

- AI SDK's retry loop sits above the middleware.
- `src/lib/llm/ledger.ts:105-131` writes a row in its `catch`, so today's TS ledger records each
  failed attempt as its own row. The Python ledger will not, as section 5 says.

**Correction 1: the Google clause.** "google-genai 2.28 still uses legacy httpx" is true of the
package on its own, but not of how Foray will run it.

- google-genai requires `httpx<1.0.0,>=0.28.1`. A bare `genai.Client()` builds
  `google.genai._api_client.AsyncHttpxClient`, a subclass of `httpx.AsyncClient` [ran].
- But google-genai 2.28 also imports `httpx2` optionally and accepts its types
  (`_api_client.py:80-120`).
- pydantic-ai's `GoogleProvider()` injects `create_async_httpx2_client()` by default
  (`providers/google.py:72-88`). The runtime type is `httpx2.AsyncClient` [ran].
- **Under pydantic-ai, Gemini traffic goes over httpx2.** Only a caller-supplied legacy
  `httpx.AsyncClient` uses legacy httpx, and that path is deprecated. It is also what
  `02_providers_wire.py` did.

**Correction 2: Google retries.** Section 7 says the vendor SDK `max_retries` defaults to 2,
"matching AI SDK". That is false for Google.

- `google.genai._api_client.retry_args(None)` returns `stop_after_attempt(1)` [source].
- pydantic-ai passes `retry_options=None` unless you set it.
- [ran]: `google: 1x429 then 200, default retry_options → http_attempts=1 wrapper_rows=['error:ModelHTTPError']`.
- So **Gemini calls get zero retries by default**, where AI SDK gives two. Set
  `GoogleProvider(retry_options=HttpRetryOptions(attempts=3, …))` for parity. Those retries also
  run below the wrapper [ran]:
  `HttpRetryOptions(attempts=3) → http_attempts=3 wrapper_rows=['ok']`.

### Claim 3: LiteLLM 1.82.7 and 1.82.8 compromised on 2026-03-24 (TeamPCP)

**Confirmed [ran: advisory database and PyPI API] + [docs].**

- **OSV.** `POST api.osv.dev/v1/query` for litellm 1.82.7 and 1.82.8 returns
  `PYSEC-2026-2` / `MAL-2026-2144`.
  - Both were published 2026-03-24 and list affected versions `['1.82.7','1.82.8']`.
  - The details read "credential harvesting malware", exfiltrating to `models.litellm[.]cloud`.
  - 1.82.6 has no MAL advisory.
- **PyPI JSON.** 1.82.7 and 1.82.8 are no longer listed; `/pypi/litellm/1.82.7/json` returns 404.
  The releases jump from 1.82.6 (2026-03-22) to 1.83.0 (2026-03-31).
- **LiteLLM's own security update** (docs.litellm.ai/blog/security-update-march-2026):
  - The bad versions were live from 10:39 UTC for about 40 minutes before PyPI quarantined them.
  - 1.78.0–1.82.6 are verified clean, and 1.83.0+ comes from a hardened pipeline.
- **Nuances:**
  - LiteLLM's post does not name TeamPCP. It links the attack to the Trivy compromise. The TeamPCP
    attribution is the group's own claim, as reported by NetSPI and Wiz (OSV references
    `wiz.io/blog/threes-a-crowd-teampcp-trojanizes-litellm…`).
  - Only **1.82.8** shipped the `.pth` file (`litellm_init.pth`). 1.82.7's payload was injected
    into `litellm/proxy/proxy_server.py`. Section 11's wording, "a `.pth` credential stealer",
    should say so.

### Claim 4: pydantic-ai release facts

**Confirmed [ran] for the dates and the `openai:` mapping; [docs] for the version policy.**

- **PyPI JSON upload times:**
  - `pydantic-ai` and `pydantic-ai-slim` 2.54.0: 2026-10-03T03:38Z.
  - 2.0.0: 2026-06-23T15:48Z (betas b1–b7 ran 2026-05-21 to 06-10).
  - 2.54.0 is the latest release. v1 is still patched: 1.107.7 came out 2026-09-30.
- **The `openai:` mapping** (`v4_openai_prefix.py`):

  ```
  1.107.7: openai:gpt-5 -> OpenAIChatModel
  2.0.0:   openai:gpt-5 -> OpenAIResponsesModel
  2.54.0:  openai:gpt-5.6-luna -> OpenAIResponsesModel, wire path ['/v1/responses']
  ```

  The changelog says the same [docs]: "The bare `openai:` model prefix now uses the OpenAI
  Responses API" (since v2.0.0b1).
- **Version policy [docs].**
  - It says: "We will not intentionally make breaking changes in minor releases."
  - Exemptions: bug fixes that relied on undocumented behaviour, new optional fields, and
    OpenTelemetry span attributes. Anything under a `beta` module is excluded entirely.
  - Deprecations last until the next major, which comes at least 3 months after v2.0.
  - v1 gets security fixes for at least 6 months after v2 stable.
  - So "minors avoid intentional breaking changes" is accurate. Keep the `~=2.54.0` pin plus the
    golden tests, because the policy itself warns that apparently safe fixes will break someone.

### Sources added in this pass

- https://pypi.org/pypi/pydantic-ai/json, https://pypi.org/pypi/litellm/json (release and upload metadata)
- https://api.osv.dev/v1/vulns/PYSEC-2026-2, https://api.osv.dev/v1/vulns/MAL-2026-2144
- https://docs.litellm.ai/blog/security-update-march-2026
- https://www.netspi.com/blog/executive-blog/ai-ml-pentesting/litellm-supply-chain-compromise/
- https://pydantic.dev/docs/ai/project/version-policy/ , https://pydantic.dev/docs/ai/project/changelog/
