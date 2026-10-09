# Step 6: deviations and clarifications

Later steps read this before the design. Each entry: what, why, evidence.

Step 6 owns `backend/apps/chat/{prompt,tools,agent,follow_up,views,tasks,api,fakes}.py`,
`backend/tests/chat/test_{chat_view,follow_up_db,follow_up_text,prompts}.py` and
`src/lib/chat/follow-up-kinds.ts`.

## 1. A tool call the model got wrong shows one plain sentence (3E §7, decided here)

- **What.** `views.ui_chunk` replaces the `errorText` of every `tool-input-error` chunk (invalid
  arguments, or a tool name that does not exist) with `INVALID_TOOL_INPUT`, "Its input was not
  valid, so it did not run.". The stored part carries the same sentence. pydantic-ai's own text
  (the pydantic validation report plus "Fix the errors and try again.", or "Unknown tool name: …")
  still goes back to the model as its retry prompt; only the UI and the row get the sentence.
- **Why.** Step 3E left the choice to this step. pydantic-ai's text is a JSON validation dump,
  which `tool-part.tsx` would print under the failed tool line; §5.2 keeps raw pydantic text away
  from users, and the TS route showed "An error occurred.". The model loses nothing: it saw the
  full report in the turn, and the converter replays the stored part as "Error: <sentence>".
- **Evidence.** `test_chat_view.py::test_ts_reference_turns_through_the_view[invalid_tool_input]`
  and `[unknown_tool]` (declared `ALLOWED_DIFFS`, below).

## 2. The TS reference turns, replayed through the real view (the 3E handoff)

- **What.** `test_chat_view.py` replays 10 of the 12 step 0 UI-stream goldens from their scripted
  `LanguageModelV4` steps (`ReplayModel`, a `FunctionModel` that also reports the step's finish
  reason) against the capture's stand-in tools behind Foray's own error wrapper, through the real
  `chat_view`, `pump_turn` and `ForayVercelStream`. Stream and saved message must equal the TS
  route's after normalisation, and every stream also passes the Node contract script.
  - **Normalised on both sides:** text and reasoning part ids (uuids here, `t1`/`r1` there) by
    order of appearance; the reply id; `tool-input-start`/`tool-input-delta` dropped (pydantic-ai
    announces every call that way, the capture only the streamed ones; both are optional chunks);
    a run of consecutive `tool-output-available` chunks sorted by call id (pydantic-ai runs
    parallel calls concurrently and reports each when ready; the client places each by id).
  - **`ALLOWED_DIFFS`:** `tool_raises` (§7.2: the error wrapper's `{ok:false, message:"That step
    failed on our side."}` as the output, where TS sent `tool-output-error` "An error occurred."),
    `invalid_tool_input` (`tool-input-error` with §1's sentence and no `tool-output-error`),
    `unknown_tool` (a static `tool-no_such_tool` part where the AI SDK used a `dynamic-tool` part).
  - **Not replayed:** `approval_turn1/2` (§7.6: approvals live in the database, the AI SDK's
    tool-approval protocol is unused) and `step_cap` (§7.1: Foray's cap is graceful; covered by
    `test_the_step_cap_ends_in_text`).
- **Evidence.** All 10 replays pass, `stream_error` and `length_finish` byte-equal after
  normalisation, with the TS route's "An error occurred." for the provider error.

## 3. The chat tools' input models are not registered for OpenAPI yet (handoff)

- **What.** `chat.api` registers the 16 tool **output** models (`tools.OUTPUT_MODELS`, camelCase
  `ApiModel`s with `extra="forbid"`; every tool test validates its output against them). The 16
  **input** models (`tools.INPUT_MODELS`) are the tools' own JSON schemas, but they are not in the
  registry.
- **Why.** Tool inputs keep today's snake_case argument names (`name_or_url`, `run_id`,
  `declined_slots`, …), and `tests/core/test_wire_format.py::test_camel_case_response_and_schema`
  fails on any component with a snake_case property that is not in `SNAKE_CASE_COMPONENTS`. That
  file is step 1's.
- **Also.** To keep snake_case components and name clashes out of `openapi.json`, the output
  models type the embedded evaluations (`fastEval`, `deepEval`), the raw preferences
  (`preferences`, `saved`) and approval `items`/`estimate` as plain objects.
- **Handoff (step 7 or 8D).** Add each `INPUT_MODELS[name].__name__` (`GetResumeInput`, …,
  `TailorResumeInput`, plus `SalaryFloorInput`) to `SNAKE_CASE_COMPONENTS` ("§7.2: tool arguments
  keep today's snake_case names"), then register them in `chat/api.py` next to the outputs
  (`register_schema(model)` for each of `INPUT_MODELS.values()`), so §6.6's `chat-types.ts` can
  name both.

## 4. Two tests pin step 1's 503 stub (handoff; the full suite has 2 failures until applied)

- **What.**
  - `tests/core/test_wire_format.py::test_chat_stub_answers_503` (step 1's file) posts `{}` and
    expects 503 "Chat is not built yet.".
  - `tests/accounts/test_auth.py::test_sliding_session_rules` (step 3A's file, line 515) asserts
    `chat.status_code == 503` for `client.post("/api/chat", {})`.
- **Why.** Step 6 replaces the stub. A form-encoded `{}` is not a readable chat request, so the
  view answers `400` "That request could not be read." (plain text).
- **Suggested fix.** Delete `test_chat_stub_answers_503` (or assert 400 and that body); in
  `test_sliding_session_rules` assert `chat.status_code == 400`. The rest of that test (no
  `Set-Cookie`, no stamp: `/api/chat` never slides) holds unchanged.
- **Evidence.** Full suite: 1649 passed, 2 skipped, exactly these 2 failed.

## 5. Choices the design left open in the view (§7.3)

- **Order.** The `chat:<user>` throttle runs in autocommit after the user is resolved and before
  the transaction (a rollback would otherwise undo the count; a request that is not signed in is
  not counted). Steps 3–7 run in one transaction, so a refused message leaves nothing behind,
  not even the claimed conversation.
- **Refusals** (plain text): unreadable JSON or no `id` → 400 "That request could not be read.";
  no message → 400 "No message provided." (today's); a message that is not `{id, role:"user",
  parts:[…]}` → 400 "That message could not be read."; no text → 400 "That message has no
  text."; over 20,000 characters → 400 "That message is too long (20,000 characters at most)."; a
  body over 2 MB → 413 with the same sentence; GET → 405 "Use POST."; anything unexpected → 500
  with §5.2's sentence. A chat id must be a canonical UUID (`{…}` and `urn:` forms are refused).
- **Regenerate.** "The latest user message" is the latest row with role `user`, run-update
  notices included (the client's `regenerate()` resends whatever user-role message is last).
- **The SDK's default body** (`{id, messages, trigger}`) works: the last message is the one sent.
- **A reply with no parts is not saved.** A turn refused before its first step (the budget guard
  refuses before the provider is called) streams `start`, `error`, `finish-step`, `finish` and
  stores only the user's message. The contract script accepts that stream.
- **An `Abort` before any chunk** (a setup failure, a timeout before `start`) sends a `start`
  with the reply id first, so the client adopts the id the row is saved under.
- **Timeouts and the ping interval** are module globals read at call time
  (`CHAT_TURN_TIMEOUT_S`, `PING_EVERY_S`, `follow_up.FOLLOW_UP_TIMEOUT_S`), for the tests.

## 6. Tools: the error wrapper, retries, and wording

- **The wrapper** (`tools.guard`, applied by `@chat_tool`): `ApiError` → its message; any
  `UserFacingError` → its text (`BudgetExceeded`'s `user_message` included); anything else →
  logged, "That step failed on our side.". `chat_tool(description, toolset=…)` lets the tests
  register stand-ins behind the same wrapper.
- **Tool retries are 8, not pydantic-ai's 1.** With the default, a second invalid call to the
  same tool raised `UnexpectedModelBehavior` and ended the turn with "An error occurred."; the AI
  SDK had no per-tool limit. 8 is the step cap, so the cap and `request_limit` decide, as before.
- **Tool descriptions are today's**, except `add_company`, which gains one sentence ("Runs in
  the background: it returns a run id at once, and the chat shows a live card."), because it now
  starts a run instead of answering inline.
- **Calls through the services of steps 5A–5C as their deviations describe:** `start_search_run`
  (`assert_can_search` then `start_search`, one `db()` call), `evaluate_job`
  (`resolve_or_create_match`, then the `evaluate:<user>` throttle, then `start_match_evaluation`),
  `tailor_resume` (`pipeline.start_tailor_run`, which throttles itself), `add_company` and
  `discover_companies` (`start_add` with its default `refresh_after=True`, and `start_names`).
- **`update_profile_preferences`** merges the given fields over the stored ones as TS did (no
  trimming; an empty list does overwrite) under a row lock, and works before a resume exists.
- **`get_job`'s salary** prints a bound whenever it is not None (`js_num`), as TS's numeric
  strings did ("0" was truthy there); `list_jobs`' card rounds like `Math.round`.

## 7. Follow-ups: choices the design left open (§7.7)

- **Nobody to tell.** An event with no run, or a run whose chat was deleted (FK `SET NULL`),
  returns quietly with no `chat.followup` event, as TS did: there is no conversation to clear.
  The other skips (`cancelled`, `decided`, `duplicate`, `stale`, and `gone` for a chat deleted
  between reading and posting) emit `chat.followup {conversationId, runId, skipped:true, reason}`.
- **The post re-checks the marker with the same 200 s rule** as step 2, so a dead process's
  marker cannot hold a follow-up forever.
- **A concurrent second post** for the same event hits `messages_one_notice_per_event` inside a
  savepoint and is reported as the `duplicate` skip.
- **Errors in the headless run** become an `error` chunk (the stream class), so a reply with no
  content is the `failed` case (`chat.followup {failed:true}`, "No chat follow-up was needed."); a
  bug outside the stream class is logged and announced the same way. Nothing is written in
  either case (the notice used to be written first and deleted).
- **`describe_update`** takes small views (`RunView`, `EventView`, `ApprovalView`), so the 21
  goldens run without rows; it still handles the legacy `tailor.done`/`tailor.failed` kinds.

## 8. The `chat` fake (§8.9)

- **What.** `apps/chat/fakes.py` plans per prompt (notice → `get_run_status` then two sentences;
  find/search → `start_search_run`; yes/approve → `list_pending_approvals`, `answer_approval`;
  resume → `get_resume`; "add <name>" → `add_company`; else a short text), skipping any step whose
  tool is not offered. A structured request on the `chat` purpose (`discover_companies`' name
  proposal) returns None, so the schema-driven default answers `{companies: []}` and the tool
  says "Could not think of matching companies.".
- **Why the empty proposal.** Proposing real names on fakes would make `start_names` run real
  discovery over the network for invented reasons; an empty proposal keeps `LLM_FAKE` runs local.

## 9. Test isolation: the R13 pool test restores the pool itself

- **What.** `test_live_answers_while_streams_open` resizes Django's per-process pool to 2 and swaps
  `core.db.DB_EXECUTOR` for a 2-thread executor, and puts both back in its own `finally`, closing
  the small pool, before the test's teardown runs.
- **Why.** With `monkeypatch`, the teardown's database flush recreated the pool while it still
  had `max_size=2`, and the rest of the session ran on 2 connections: `tests/jobs/test_careers_page.py`
  then timed out (`PoolTimeout`). A scratch pytest plugin printing the pool after each test showed
  `max=2` from that test on; with the fix it stays `max=10`.

## 10. Live checks run in this step

All through `:3000` with `pnpm dev:all`, a `create_test_user --with-resume` user and a
`pnpm dev:session` cookie, `JSEARCH_ENABLED=false` in the process environment. **$0 spent**: step
6 states no money ceiling, so every live turn ran on `LLM_FAKE=1`.

- **Streaming** (`LLM_FAKE_DELAY_MS=1500`): "What's on my resume?" with
  `Accept-Encoding: gzip` streamed `start` at +0.11 s, the `get_resume` call at +1.6 s and 20 text
  deltas from +1.73 s to +3.18 s, `[DONE]` at +3.20 s; no `content-encoding` header,
  `cache-control: no-cache, no-transform`, `x-conversation-id` set. Rows: the user message and the
  reply; 2 ledger rows `chat|fake|label fake|$0` carrying the `conversation_id`; title set, marker
  cleared.
- **Search follow-up at gate 1** (no delay): "Find me backend roles" started a search run with
  the conversation; it reached `awaiting_approval`, and `GET /api/conversations/{id}` then held the
  notice (`run_update`, label "Search paused for your go-ahead") and the reply (`follow_up`):
  `get_run_status` (a follow-up tool) then "Foray checked the run: it is awaiting_approval. …".
- **"add linear.app"** started a `company_discovery` run with the conversation; it completed and
  the chat got the notice "Discovery finished" and a reply.
- **Yielding to a streaming turn** (`LLM_FAKE_DELAY_MS=8000`): a turn from 05:07:31 to 05:07:47
  started an add that completed at 05:07:40. The worker logged "chat follow-up for event 244: a
  turn is streaming; trying again in 4s" at 05:07:40 and 05:07:45, then posted at 05:08:06, after
  the turn: notice and reply have higher `seq` than the turn's reply.
- **Budget** (`LLM_MONTHLY_BUDGET_USD=0.000001`, month spend $0.270806): the turn streamed
  `error` "This month's model budget of $0.000001 is spent. Raise LLM_MONTHLY_BUDGET_USD to
  continue." and no ledger row was written.
- **Afterwards:** each worker logged `stopped`; `create_test_user --purge` printed "Deleted 1 test
  user."; `foray_app` has 0 users, 0 sessions, 0 worker rows, 0 todo/doing jobs, 0 active runs;
  :3000, :8000 and :8001 are free. Legacy `foray` write counters stayed at 41158/1014040/5351.
