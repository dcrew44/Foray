# Step 3B: deviations and clarifications

Later steps read this before the design. Each entry: what, why, evidence.

## 1. How `LLM_FAKE` resolves and labels (clarification)

- **What.** Under `LLM_FAKE`, `registry.model_ref(purpose)` returns `fake:<purpose>`, and
  `registry.base_model("fake:<purpose>")` returns `fake.fake_model(purpose)` (only while
  `ENV.debug`). `build_stack` labels the ledger rows of a fake ref `fake` itself, overriding the
  context's label. Nothing sets `label="fake"` in the `llm_context`.
- **Why.** §8.8 lists "the fakes, with `label='fake'`" among the context setters, but a fake runs
  *inside* the request, below the ledger, so it cannot set the context the ledger reads; and
  §15.2 needs `LLM_FAKE=1 pnpm llm:smoke` to write `fake` rows although the smoke sets
  `label="smoke"`. `tests/fakes.py` refs are also `fake:<purpose>`, but without `LLM_FAKE` their
  rows keep the context's label, so tests can assert attribution labels.
- **Also.** The gate is `settings.ENV.debug`, not `settings.DEBUG`: pytest-django forces
  `settings.DEBUG = False` during tests. `LLM_FAKE` itself still requires `DEBUG=true` at boot.
- **Evidence.** `LLM_FAKE=1 pnpm llm:smoke` printed 7 ✓ lines (`fake:chat` … `fake:merge`) and
  `foray_app.llm_calls` gained 7 rows `label='fake'`, `cost_usd=0`.

## 2. The `fake.register` contract (for 3C, 3D, 5A, 6)

- **What.** An app registers overrides in its own `apps/<app>/fakes.py`; `fake.discover()` imports
  every installed app's `fakes` module once (Django's `autodiscover_modules("fakes")`), when the
  first fake model is built. `fake.register(purpose, fn)` (or `@fake.register(purpose)`) appends
  `fn(messages, info)`, sync or async, where `info` is pydantic-ai's `AgentInfo`. It returns
  `None` (not mine: try the next override, then the schema-driven default), a `dict` or pydantic
  model (the structured output), a `str` (a text reply) or a `ModelResponse` (full control, e.g.
  `fake.tool_call("get_resume", {})`). Several overrides may share a purpose (`extract`: 3C and
  3D) and are tried in registration order. Helpers: `fake.latest_user_text(messages)`,
  `fake.offered_tools(info)`, `fake.output_title(info)` (the output model's name, to tell
  `ExtractedResume` from `ExtractedPosting`), `fake.output_schema(info)`,
  `fake.minimal_instance(schema)`, `fake.text(...)`, `fake.tool_call(...)`.
- **Why.** §8.9 names the hook but not its signature; one purpose (`extract`) has two owners.
- **Evidence.** `tests/llm/test_fake.py::test_overrides_are_tried_in_order`,
  `::test_an_override_can_call_tools_and_reply_in_text`.

## 3. `structured_result()` beside `structured()` (addition)

- **What.** `structured()` keeps the §8.4 signature and returns `T`. `structured_result(...,
  model=None)` returns `StructuredResult(output, usage: RunUsage, retried)` and accepts an
  explicit model stack; `structured()` calls it.
- **Why.** `llm_smoke` prints `in=… cached=… out=…` and `(retried)`, and `llm_bakeoff` sums
  tokens per model and runs explicit refs (`registry.build_stack(purpose, ref, effort=…)`).
- **Evidence.** `tests/llm/test_structured.py`, `tests/llm/test_commands.py`.

## 4. Ledger details the design left open

- **Timeouts are cancellations at the ledger.** `asyncio.timeout` cancels the task, so a timed-out
  request reaches `LedgerModel` as `CancelledError`; its row reads `cancelled` (or
  `cancelled (input estimated)` when no usage had arrived). A provider-side timeout
  (`APITimeoutError`) is recorded as its `repr`. `GeneratorExit` (a closed stream consumer) also
  counts as a cancellation.
- **"No usage" means no input of any kind** (uncached, cache-read and cache-write all 0).
- **The estimate** is `ceil((len(ModelMessagesTypeAdapter.dump_json(messages)) +
  len(InstructionPart.join(params.instruction_parts))) / 4)`; the JSON envelope makes it err high,
  which is the safe side for the budget.
- **`cost_usd`** is `pricing.to_column(cost)`: the exact binary float rounded half up to 6 places,
  as `toFixed(6)` did.
- **Evidence.** `tests/llm/test_ledger.py::test_ledger_matrix`,
  `::test_cancelled_call_without_usage_is_estimated`, `::test_estimate_counts_messages_and_instructions`.

## 5. Budget guard additions

- **What.** `BudgetExceeded` subclasses `apps.core.errors.UserFacingError` (`str(exc) ==
  user_message`), so code that only knows `UserFacingError` still shows the right sentence. The
  budget is printed without exponent or trailing zeros (`$90`, `$2.5`, `$0.000001`). Each ledger
  row this process writes is added to the cached month-to-date sum (`budget.note_spend`), so a
  burst cannot overrun the budget by a whole 30 s window of its own spend.
- **Evidence.** `tests/llm/test_budget.py::test_own_spend_updates_the_cached_sum`;
  `LLM_MONTHLY_BUDGET_USD=0.000001 pnpm llm:smoke` exited 1 with "This month's model budget of
  $0.000001 is spent. Raise LLM_MONTHLY_BUDGET_USD to continue." on all 7 lines and
  `llm_calls` stayed at 7 rows.

## 6. `llm_context` replaces, and validates its label

- **What.** A nested `llm_context` block replaces the outer context (no field merging), as
  `AsyncLocalStorage.run` did. A label other than `fake`/`smoke`/`bakeoff` raises `ValueError`
  at the block.
- **Why.** `llm_calls.label` has a CHECK constraint (step 2): a bad label would make the insert
  fail and the row be lost (only logged).
- **Evidence.** `tests/llm/test_ledger.py::test_labels_are_the_column_choices`.

## 7. Quirks: temperature is dropped only while thinking is on

- **What.** `reasoning.quirks_for(ref)` marks Anthropic `sonnet-5|opus-5|haiku-4-5`;
  `PurposeSettings` drops `temperature` for them when the merged settings think (`thinking` set and
  not `False`). TS's `supportsTemperature` dropped it regardless (no call site passed one).
- **Why.** §8.2: "dropping `temperature` on Anthropic Haiku 4.5 when thinking is on".
- **Evidence.** `tests/llm/test_wire.py::test_anthropic_haiku_drops_temperature_when_thinking`.
- **Also.** A caller's `extra_body` merges key by key with the purpose's, so it can never drop
  DeepSeek's `thinking` switch (`::test_deepseek_keeps_thinking_when_the_caller_adds_extra_body`).

## 8. Commands

- `llm_smoke` refuses an unknown purpose (`CommandError`, exit 1); TS silently ran nothing. The
  unpriced hint now names `apps/llm/pricing.py`. `fake:*` refs count as priced.
- `llm_bakeoff` exits 2 for a malformed ref, an unknown purpose or an unknown `--reasoning`
  value (TS printed "No fixtures" and exited 1 for an unknown purpose, and passed any effort
  through), and 1 for an unknown or keyless provider. Under `LLM_FAKE` it refuses real refs
  (exit 1) and runs `fake:<purpose>` refs, so a bake-off can be exercised for $0 but never
  spends while fakes are on. The table reproduces Node 26's `console.table` (index column,
  quoted values); `toFixed` and `Math.round` are ported exactly.
- A budget refusal ends a bake-off (§8.6). The fixture counts any other failure as a schema
  failure, as TS did, but re-raises `BudgetExceeded`, which says nothing about the model;
  `llm_bakeoff` then prints the guard's sentence on stderr and exits 1, with no table (a partial
  one would rank models on a sample the budget cut short). Evidence:
  `tests/llm/test_commands.py::test_bakeoff_over_budget_exits_1` (spend $1, budget $0.5: exit 1,
  0 provider calls, no new ledger rows); without the re-raise it exited 0 with a `3/3`
  schemaFail row. Found by verification round 1.
- The pydantic-ai first-run banner is switched off in `LlmConfig.ready()`
  (`pydantic_ai.BANNER_ENABLED = False`) instead of `PYDANTIC_AI_NO_BANNER`, so nothing reads or
  writes `os.environ`.

## 9. The JS formatters diverge outside the salary domain (handoff, not fixed here)

- **What.** `tests/llm/test_jsonio.py` checks `apps.core.jsonio` (step 1) against values printed
  by Node 26. Every salary-like value matches. Ten cases fail and are marked
  `xfail(strict=True)`:
  - `js_num` prints exponent notation for `1e-6 <= |x| < 1e-4` (`1e-5` → `1e-5`; JS `0.00001`),
    because `repr` switches below `1e-4` and JS below `1e-6`;
  - `js_num` prints the exact integer of an integral float above 2^53 whose shortest form has
    fewer digits (`1.2345678901234567e19` → `12345678901234567168`; JS `12345678901234567000`);
  - `js_locale` drops the sign of a negative value that rounds to zero (JS prints `-0`);
  - `js_locale(1.7976931348623157e308)` raises `decimal.InvalidOperation` (the default 28-digit
    context cannot quantize it).
- **Why not fixed.** `apps/core/jsonio.py` belongs to step 1; this step owns only its tests.
- **Suggested fix.** `js_num`: for finite `x` with `1e-6 <= |x| < 1e21`, print
  `format(Decimal(repr(x)), "f")` with a trailing `.0` removed; otherwise the current exponent
  path. `js_locale`: quantize inside `decimal.localcontext()` with `prec` large enough
  (e.g. 400), and keep `-` when `x < 0` even if the rounded value is zero. When fixed, the strict
  xfails start passing and their markers must be removed.

## 10. Live checks run in this step

- `pnpm llm:smoke` (real `.env`, providers `openai, deepseek`): 7 ✓ lines, 7 rows
  `label='smoke'`, no user, total `$0.001460` (chat 0.000068, extract/score/verify/merge 0.000045
  each, evaluate/tailor 0.000606 each). `score` reported 0 reasoning tokens
  (`LLM_REASONING_SCORE=none`).
- `LLM_FAKE=1 LLM_FAKE_FAIL=score pnpm llm:smoke score`: `✗ score     fake:score: Fake failure`,
  exit 1 (it also writes one `fake` row with the error, as every failed request does).
- Legacy `foray` write counters stayed at 41158/1014040/5351.
