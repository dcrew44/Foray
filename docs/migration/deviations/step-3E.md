# Step 3E: deviations and clarifications

Later steps read this before the design. Each entry: what, why, evidence.

## 1. The view iterates `ForayVercelStream(turn).chunks(events)`, not `transform_stream`

- **What.** `ForayVercelStream(turn)` takes the reply id from `turn.reply_id`
  (`ForayVercelStream(message_id=...)` names it directly; neither given raises `ValueError`).
  `chunks(events)` yields wire dicts (camelCase, `None` dropped, required keys kept) from `start`
  to `finish` and **no `[DONE]`**, so `stream_turn` can `acc.apply(item)`, test
  `item["type"] == "finish-step"` and `frame(item)` exactly as §7.3 writes it, and send `DONE`
  itself after the save. `transform_stream` is pydantic-ai's and still yields `BaseChunk`
  objects, ending with a `DoneChunk`; `encode_event` frames those (`DoneChunk` → `DONE`).
- **Why.** §7.3's pseudo-code treats the items of `transform_stream` as dicts. Overriding
  `transform_stream` to yield dicts would break the base class's own `encode_stream` typing
  (pyright: incompatible override).
- **Evidence.** `tests/chat/test_uistream.py::test_function_model_turns`,
  `::test_stream_takes_the_reply_id_from_the_turn`; §15.4's override points confirmed by
  `::test_override_points_exist_in_pydantic_ai_2_54_0` (`encode_event`, `before_stream`,
  `handle_run_result`, `on_error`, same signatures as `VercelAIEventStream` 2.54.0).

## 2. `BudgetExceeded` is recognised by its `user_message`

- **What.** `agent_error_text(exc)` (what `on_error` shows) returns the `user_message` of any
  exception that carries a non-empty one (§8.6's `BudgetExceeded`), `str(exc)` for
  `apps.core.errors.UserFacingError`, the step-limit sentence for `UsageLimitExceeded`, and
  otherwise logs the traceback and returns "An error occurred.". It also looks inside exception
  groups and at explicit `__cause__`s, first match wins.
- **Why.** `apps/llm/budget.py` is step 3B's, built in parallel; this lane cannot import it.
- **Evidence.** `::test_agent_error_text`; the contract's `agent_budget` scenario uses the real
  class when `apps.llm.budget.BudgetExceeded(user_message=...)` exists, a stand-in otherwise
  (`tests/contract/scenarios.py::budget_exceeded`). Once 3B has merged, the real class is
  constructed as `BudgetExceeded(user_message=...)`; if its signature differs, that helper raises
  `TypeError` and the tests fail loudly rather than falling back to the stand-in.

## 3. "Byte for byte" needs JavaScript's `undefined` slots

- **What.** The accumulator keeps a JS-faithful `state` in which keys TS sets to `undefined`
  hold `UNDEFINED`: they keep their position, which fixes the key order `JSON.stringify` writes.
  `acc.message` drops them (a deep copy: what the view saves). The approval goldens' turn 2
  continues from turn 1's `state` with the approval applied, because the TS capture passed its
  live object (spread, `undefined` slots included) back as turn 2's last message. Continuing from
  stored JSON gives the same content with `output` after `approval`, which is also what TS does
  with that input.
- **Why.** The prototype's `test_accumulator.py` compared dicts with `==`, which ignores key
  order; the Done check asks for bytes.
- **Evidence.** `::test_accumulator_reproduces_ts_reference_turns` (both prototype turns) and
  `::test_accumulator_reproduces_every_golden` (all 14 step 0 goldens) compare
  `JSON.stringify(message, null, 2)` output byte for byte; `::test_continuation_from_stored_json_matches_ts_order`.

## 4. Additions the design implies but does not spell out

- **`frame()` also keeps `data` as `null`** on `data-*` chunks: like `output`, it is a required
  `z.unknown()` (`ai/src/ui-message-stream/ui-message-chunks.ts:179`).
- **`UIMessageAccumulator.close_with_error(text)`** (called by §7.3's `stream_turn` on `Abort`):
  `text-end`/`reasoning-end` for open parts, `error`, `finish-step` if a step is open, and
  `finish{finishReason:"error"}` unless the turn already finished. It returns the chunks, already
  applied. Tool parts are left as they are (the converter gives them the "interrupted" return).
- **`parsePartialJson`/`fixJson` are ported**, so a tool part cut off by Stop is saved with the
  partial input the client showed (the prototype skipped this).
- **Headers:** `UI_HEADERS` plus `ui_headers(conversation_id)` (adds `X-Conversation-Id`);
  `PING = ": ping\n\n"`, `DONE`, `GENERIC_ERROR`, `STEP_LIMIT_ERROR`.
- **Size.** `uistream.py` is about 1,000 lines, not about 300: it ports every chunk kind of
  `processUIMessageStream` (approvals, data, sources, files, `custom`, `reset-step`) and `fixJson`
  state for state, which byte equality and the partial-input contract cases need.

## 5. Converter details within §7.4

- **Merging.** Any two consecutive requests merge, not only two user messages: a step's tool
  returns followed by the next user message become one request (pydantic-ai 2.54.0 normalises
  history the same way before each model request).
- **Window.** First user message + the latest messages within 40 / 60,000 characters, the latest
  message always kept. When older messages were cut (a gap after the first message), the kept
  tail starts at a user message, so the model never sees a reply without its request; without a
  gap nothing is dropped. Size = text plus the JSON of tool `input`/`rawInput`/`output` and the
  `errorText`.
- **Returns.** `output-error` → `"Error: <errorText>"` with pydantic-ai's default outcome;
  `output-denied` → `"Error: The user declined this call."`; any state without an output, or an
  unknown state → the interrupted sentence. Non-object tool input is passed as
  `{"input": <value>}`.
- **§15.4 confirmed:** `run_stream_events(user_prompt=..., message_history=...)` accepts the
  converted history, including a history that ends in a request (the trailing request and the
  new prompt are merged); the fallback (`message_history` only, `user_prompt=None`) works too
  (`tests/chat/test_convert.py::test_pydantic_ai_accepts_*`).

## 6. Contract comparison rules

- **What.** `validate_stream.mjs` compares the client's last written snapshot with the persisted
  message by deep equality after dropping trailing `step-start` parts from both.
- **Why.** `start-step` never calls `write()` in `processUIMessageStream`, so a `step-start` with
  nothing after it is in the server's copy but not in the client's last snapshot; key order is not
  compared because `jsonb` does not keep it.
- **Evidence.** `tests/contract/test_contract.py::test_contract_rejects_what_the_client_rejects`
  shows the check fails on a null optional, a missing `output`, NaN, an unknown chunk type, an
  orphan delta and a wrong persisted message.

## 7. Observed, left for step 6 to decide (no change here)

- pydantic-ai's `tool-input-error` text is the validation report plus "Fix the errors and try
  again."; TS sent "An error occurred.". The UI shows it under the failed tool line.
- Text, reasoning and tool chunks carry `providerMetadata: {"pydantic_ai": {...}}` when the
  provider reports ids or signatures; it validates and is saved (TS saved its provider metadata
  too); the converter drops it on replay.
