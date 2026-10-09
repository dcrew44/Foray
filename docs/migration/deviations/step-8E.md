# Step 8E: deviations and clarifications

Later steps read this before the design. Each entry: what, why, evidence.

Step 8E closes the API gaps that 8B, 8C and 8D recorded, plus the open handoffs (a) to (i) of its
instructions. It owns the backend files those fixes touch, `backend/openapi.json` and
`src/lib/api/schema.d.ts`. It also edits five frontend files, because the instructions ask for it:
`src/lib/api/chat-types.ts` (8D's) and `src/lib/api/server.ts` (3G's), and, so the frontend drops
its hand-written run-output types, `src/lib/api/types.ts` (8A's) and three of 8C's components.

Files changed:

- **backend:** `apps/chat/{api,fakes,tools}.py`, `apps/core/{queue,throttle}.py`,
  `apps/jobs/{pipeline,schemas}.py`, `apps/pages/build.py`, `apps/resumes/models.py` (a comment),
  and `apps/runs/{api,approvals,catalogue,live,step,tasks}.py`;
- **backend tests:** `tests/chat/{test_chat_view,test_follow_up_db}.py`,
  `tests/core/{test_queue,test_wire_format}.py`, `tests/jobs/{test_ingest,test_pipeline}.py`,
  `tests/pages/test_pages.py` and `tests/runs/{test_reaper,test_tasks}.py`;
- **frontend:** `src/lib/api/{chat-types,server,types}.ts`,
  `src/components/companies/add-company.tsx`, `src/components/profile/resume-dropzone.tsx` and
  `src/components/settings/worker-panel.tsx`;
- **generated:** `backend/openapi.json` and `src/lib/api/schema.d.ts`. They are regenerated and
  staged (`git add`), as step 7 did, so the drift step's `git diff --exit-code` compares a fresh
  regeneration with them.

No migration was added.

## 1. (a) `test_sliding_session_rules` already asserts 400

- **What.** Nothing changed. Integration G4 applied this handoff (integration-G4.md §1).
  `tests/accounts/test_auth.py` line 515 asserts `chat.status_code == 400` for
  `client.post("/api/chat", {})`.
- **Evidence.** The full suite passes (§9).

## 2. (b), (f), (i): what the OpenAPI schema now names

### 2.1 Chat tool inputs (b)

- **What.**
  - `apps/chat/api.py` registers every `INPUT_MODELS` value plus `SalaryFloorInput` next to the
    outputs. `SalaryFloorInput` is now in `tools.__all__`.
  - `tests/core/test_wire_format.py` lists the 17 names in `SNAKE_CASE_COMPONENTS`, each with the
    reason `TOOL_ARGUMENTS` ("§7.2: tool arguments keep today's snake_case names").
  - The new `test_chat_tool_inputs_are_exported_with_their_argument_names` checks four things:
    - the listed names are exactly the input models;
    - each is exported;
    - each component's properties are the model's field names;
    - each has `additionalProperties: false`.
  - In `src/lib/api/chat-types.ts`, the `TODO(8E)` block is gone. `ForayTools` names
    `S["<Name>Input"]` for all 16 tools: `JobIdInput` became `GetJobInput`, `EvaluateJobInput` and
    `TailorResumeInput`; `NoInput` became `GetProfileSummaryInput` and
    `ListPendingApprovalsInput`. Nothing else imported the hand types.
- **Note.** The inputs' enums are `Literal`s in `tools.py`, so the schema inlines them
  (`"intern" | "new_grad" | …`). It does not refer to `S["Seniority"]`. `tsc` accepts every
  existing use.

### 2.2 Run outputs and event data (i)

- **What.**
  - `apps/runs/api.py` registers `ImportOutput`, `CompanyAddOutput` and the new `WorkerPingData`.
  - `WorkerPingData {jobId, workerPid}` is defined in `catalogue.py` under "events.data".
    `tasks.worker_ping` now emits it, so its shape is defined once.
  - Their `$defs` add `DiscoveryStep`, `GarbleReport` and `ImportCounts`.
- **`CompanyAddOutput.company` is typed as `CompanyRow`.**
  - The catalogue cannot import `jobs.pipeline`, which imports the catalogue. So `CompanyRow`
    moved from `jobs/pipeline.py` to `jobs/schemas.py`, where §2.3's JSON shapes live.
  - `pipeline` re-exports it, so every importer is unchanged.
  - `last_fetched_at` is `IsoDatetime`: a `PlainSerializer(when_used="json")` writes
    `toISOString()`, and `WithJsonSchema` keeps the date-time schema. A stored run output's row
    is therefore byte-identical to `company_row()`'s, as before. ninja's own responses do not use
    JSON mode, so they are unchanged.
- **Frontend.**
  - `types.ts` adds `ImportOutput`, `CompanyAddOutput` and `WorkerPingData`. `DiscoveryStep`
    now comes from the schema; 8A had written it by hand (step-8A.md §3).
  - `add-company.tsx` drops `AddOutput`, `resume-dropzone.tsx` its inline `{usedVision}` type, and
    `worker-panel.tsx` its `PingAnswer`.
- **Evidence.**
  - **The export.** Exported before and after, it has 23 new components and no changed one.
    `CompanyRow` is byte-identical.
  - **The stored row.** `tests/jobs/test_pipeline.py` now asserts that the add's
    `output.company` equals `pipeline.company_row(company, open_roles)`. With `last_fetched_at`
    typed as a plain `datetime | None` (a scratch edit, reverted), that test fails: pydantic would
    write microseconds.

### 2.3 `Me` (f)

`server.ts`'s `Me` is `components["schemas"]["MeOut"]`.

### 2.4 Not done (optional, still open)

- From step-8C.md §6:
  - an output-mode `Resume` component, which would make `editableResume` unnecessary;
  - `ErrorOut` 4xx response bodies, which would type `WorkerPanel`'s refused-ping cast.
- From step-8B.md §4: the four page names in `types.ts`.

## 3. (c) The R13 tests run through the real ASGI application

- **What.**
  - `test_stream_errors_leak_nothing` and `test_live_answers_while_streams_open` drive
    `foray.asgi.application` with a small in-test ASGI client (`AsgiCall`). Each request runs in
    its own task, as under uvicorn, so Django's `ASGIHandler` gives each request its own
    `ThreadSensitiveContext`. The response arrives message by message, so streams can be held
    open.
  - `AsyncClient` gives no request a context of its own, so every thread-sensitive call shares
    one thread and one connection. That is why step 6's verifier could not make either test fail.
- **The leak test.**
  - The session is made on the test's thread before any request.
  - The pool's checked-out count is measured before the first turn, then after one good turn,
    then after 20 failing turns. Each measurement must equal the first.
  - The old test measured after a warm-up turn, which hid a connection a thread keeps for good.
- **The live test.**
  - The 4 chat requests start together.
  - The pool and the `db()` executor are 2, as before, with a pool `timeout` of 3 s, so a pinned
    connection fails the test in seconds instead of hanging it.
  - `/api/live` must answer while the 4 streams are open, and every stream must end in `[DONE]`.
- **Evidence: mutations, each in a scratch copy of `backend/` outside the repo.**
  - **Setup.** The copies were `scratchpad/mut/{base,pin,leak}`, with `.venv` symlinked, on the
    test database `test_foray_app_8e_mut`. Each copy ran the new tests and HEAD's versions of the
    two tests (copied in as `tests/chat/test_chat_view_head.py`).
  - **base (unmutated):** 4 passed.
  - **pin.** `chat_view` runs `sync_to_async(prepare_turn)(…)` on the request thread, and
    `_release_request_connections` is a no-op.
    - The new leak test fails. The request threads' connections are never returned, the pool runs
      dry, and the next turn times out.
    - The new live test fails with `statuses == [500, 200, 200, 500]`: `PoolTimeout` in
      `prepare_turn`.
    - HEAD's two tests pass.
  - **leak.** `views.db` is `asyncio.to_thread`.
    - The new leak test fails with "a good turn kept a connection" (`2 == 1`).
    - The new live test fails with `[200, 500, 200, 500]`.
    - HEAD's two tests pass.

## 4. (d) Home counts only the signed-in user's month

- **What.** `tests/pages/test_pages.py::test_home_page_counts_only_this_users_month` adds three
  ledger rows to the full graph:
  - a system row (no user) costing $1.5;
  - another user's row costing $2.5;
  - the user's own $4 row from one second before `month_start()`.

  Home's `spend` must stay `{usd: 0.225, calls: 3}`.
- **Evidence (mutations of `_month_spend_brief`, each reverted).**
  - `for_user` replaced by `unscoped(...)`: `{usd: 4.225, calls: 5}`, so the test fails.
  - The month filter dropped: `{usd: 9.225, calls: 5}`, so it fails.

## 5. (e) `Document.render_hash`'s comment

The comment in `apps/resumes/models.py` now gives both keys: the base resume's
`sha256(html_normal | RENDER_VERSION)`, and the tailored resume's
`sha256(html_normal | RENDER_VERSION | "<Name> - Resume - <Company>.pdf")` (step-H1.md §10). It is a
comment only, with no migration. `makemigrations --check` is unaffected.

## 6. (h) The chat fake lists matches

- **What.** In `apps/chat/fakes.py`, after SEARCH and APPROVE, a prompt matching
  `\b(matches|jobs)\b` calls `list_jobs` with all five arguments null, then answers "Here are your
  matches.". A refusal `{ok:false}` answers its message instead. The module table has the new row.
- **Evidence.**
  - `tests/chat/test_chat_view.py::test_the_chat_fake_lists_matches` runs "Show me my matches"
    through the real view with `fake.fake_model("chat")`. It checks the exact input, the two
    matches in the output, the parts and the sentence. The Node contract script accepts the
    stream.
  - In the pane, the card showed 10 match lines (§8).

## 7. (g) One clock per timestamp

### 7.1 The skew was gone, so it was simulated (deviation)

- **The real skew.** The instructions describe `foray-db-1` as about 8 s behind the host. During
  this step it was not: three readings of `clock_timestamp()` against the host clock gave
  +0.012 s, +0.009 s and +0.008 s, with round trips of 55–65 ms. The Mac had not slept. So under
  the current real skew the two render tests pass with or without the fix, and "still fail if the
  fix is reverted" cannot be shown at the real clock.
- **The simulation.** The skew was simulated with a scratch pytest plugin
  (`scratchpad/skew/foray_skew.py`, loaded with `-p foray_skew`, not in the repo). It runs the
  process clock `FORAY_SKEW_S` seconds ahead of the database's.
  - Shifted: `time.time`, `time.time_ns`, `django.utils.timezone.now` (so every model default and
    `timezone.now()` call) and `procrastinate.utils.utcnow`.
  - Not shifted: direct `datetime.now(...)` calls (a C type) and monotonic clocks.
  - +8 is the reported direction: Python ahead, the database behind. −8 is the opposite.

### 7.2 The cause of the two failures

- procrastinate retries a job with `scheduled_at = retry_at or utils.utcnow()`. That is Python's
  clock, plus a wait of 0.
- `procrastinate_fetch_job_v2` then compares `scheduled_at <= now()`, which is the database's
  clock.
- With the database 8 s behind, the retry sits 8 s in the database's future. `run_deferred()`
  drains the queue without it, and the render run stays `running`.

### 7.3 The fix

- `apps/runs/step.py` adds `ImmediateRetry(RetryStrategy)`. When procrastinate decides to retry
  with no wait, it returns `RetryDecision(retry_at=RETRY_DUE_NOW)`, the epoch. That is in the past
  by any clock, so the database alone decides. A strategy with a wait keeps procrastinate's own
  decision.
- `run_step(retry=N)` uses it through `immediate_retry(N)`, which is `False` for 0, so R11's
  `retry_strategy is None` holds. `shutdown_only_retry()` uses it too.
- **Side effect.** A retried job's `scheduled_at` and its `scheduled` event read 1970-01-01.
  Nothing in Foray reads them.
- **The alternative not taken.** Passing the string `'now'` as `retry_at`, which Postgres would
  read as its own transaction time. It is untyped, and it relies on procrastinate passing the
  value through untouched.

### 7.4 Evidence

- **Before the fix, at +8 s, the full suite: 8 failed, 1696 passed.**
  - the two render tests;
  - `tests/runs/test_tasks.py::test_retry_one_step_*` (2);
  - `tests/runs/test_approvals.py`: `::test_a_lapsed_approval_cannot_be_decided_…`,
    `::test_the_sweep_resumes_…` and `::test_the_sweep_lapses_quietly_…`;
  - `tests/runs/test_api.py::test_live_lists_only_answerable_approvals`.
- **The revert proof.** The scratch copies `scratchpad/mut/clk-fixed` and `clk-reverted` were
  run. `clk-reverted` has `run_step` back on `retry=retry` and `shutdown_only_retry` back on a
  plain `RetryStrategy`. Each copy ran the two render tests and the new regression test:
  - **fixed:** 3 passed at +0 and at +8;
  - **reverted at +0:** the render tests passed, and the regression test failed;
  - **reverted at +8:** all 3 failed.
- **The new regression test.** `tests/runs/test_tasks.py::test_an_immediate_retry_is_due_by_the_database_clock`
  sets procrastinate's clock 5 minutes ahead. It then needs a retry-1 run to complete, with
  attempts 2, in one drain. It also checks both strategies' decisions. It fails without the fix
  at the real clock.

### 7.5 The same mix elsewhere: fixed (cheap)

- **Deferral delays.** `core.queue.defer_in_tx(schedule_in=…)` now measures the delay from the
  database's `clock_timestamp()` and passes `schedule_at`.
  - The one caller is the follow-up's 4 s re-defer while a turn streams.
  - With the database's clock ahead of Python's by more than 4 s, that re-defer was due at once,
    so the follow-up spun until the turn ended.
  - New `tests/core/test_queue.py::test_a_delay_is_measured_from_the_database_clock` shifts
    procrastinate's clock 5 minutes back and checks that the delay is 3–4 s by the database.
  - `test_follow_up_db.py`'s redefer assertion now compares in SQL, within `(0, REDEFER_S]`.
- **Approval expiry.** `expires_at` is written with Python's clock (`request_approval`), so every
  comparison now uses Python's clock:
  - `live.snapshot`'s approvals;
  - `approvals.list_pending`;
  - `approvals.expire_lapsed` (both the candidate filter and the `FOR UPDATE` query, which now
    takes the same `now` as a parameter);
  - `pages.build._waiting`.

  Three of these used `Now()`, which is what broke the 4 approval tests at +8.
- **Recent runs.** `live.snapshot`'s runs filter uses `Now() - RECENT`. A run's final
  `updated_at` is written by `now()` in SQL (`lifecycle`).
- **Throttle pruning.** `throttle.prune` uses `Now() - older_than`. Windows start at
  `date_bin(…, now(), …)`.
- **Two tests that mixed the clocks.** `tests/jobs/test_ingest.py::test_last_seen_moves_forward_…`
  and `tests/runs/test_reaper.py::test_lifecycle_fail_is_what_the_reaper_uses` compared a
  SQL-`now()` timestamp with `timezone.now()`. They failed at −8. They now compare with the
  database's `clock_timestamp()`.
- **After all fixes, the full suite:**
  - real clock: 1706 passed;
  - +8: 1706 passed;
  - −8: 1706 passed.

### 7.6 Heartbeat and "online": no change needed

- procrastinate writes `last_heartbeat = now()` and prunes by `now()`, and `live.worker_status`
  computes `online` in SQL (§9.7). Both sides use the database's clock.
- `WorkerPanel` already tolerates a skewed `createdAt` (step-8C.md §9).
- `lastSeenAt` is shown relative to the browser's clock, so a skew shifts that one line of copy
  by the skew.

### 7.7 Still mixed (recorded, not fixed)

- **Columns written with both clocks.** Model defaults (`Timestamped`, events, approvals, ledger)
  use `timezone.now` (Python). Raw SQL (`lifecycle`, ingest, the chat claim) writes `now()` to
  some of the same columns.
- **Where that is read.**
  - The reaper's `runs.updated_at < now() - 60 s` and `acceptance_checks`' 2-minute line read a
    queued run's ORM-written `updated_at` against the database's clock.
  - `llm.budget` counts the month from SQL `now()`, over ledger rows the ORM dates.
  - In each case a skew of s seconds moves the boundary by s seconds.
- **What a fix would take.** Making one clock win needs either `db_default=Now()` on every model
  timestamp, which is a migration (step 2 alone writes them), or Python times passed into every
  raw-SQL write. Both are larger than this step. Step 10 or 11 may take it up.

## 8. The deferred 8B, 8C and 8D pane checks

### 8.1 Setup

- **Servers.** `pnpm dev`, `pnpm dev:api` and `pnpm dev:worker` ran as background processes,
  each with `LLM_FAKE=1` and `JSEARCH_ENABLED=false` in its own process environment, logging
  through a Python line timestamper. `.env` was not touched.
- **The pane.** It was opened with `preview_start {url: http://localhost:3000/sign-in}`. It was
  hidden much of the time, so the checks dispatched `foray:live-refresh` every 1.5–2 s, and a
  `MutationObserver` recorded every toast added or rewritten.
- **Users.** Two throwaway users, `e8-u1@example.test` and `e8-u2@example.test`, came from
  `create_test_user` and `dev_session`.
- **The stale cookie.** The pane's profile still held an `HttpOnly` `sessionid` from an earlier
  step, so `document.cookie` could not replace it. One `fetch('/api/me')` (401) made Django delete
  it first. That 401 is the only console error of the session.
- **The upload.** The fixture was staged as `public/e8-two-column.pdf` (deleted afterwards) and
  handed to the dropzone's own file input through a `DataTransfer`.

### 8.2 Dark (the default; no theme key set), user U1

**8C.**
- **Fresh profile.** It showed "Start with your resume", the dropzone and the Preferences form.
- **Preferences save.** Typed and clicked: a role, "Mid level", a location, Remote and work
  authorisation. The page toasted "Preferences saved", the SaveBar was gone, and the description
  switched to "Every search, score and deep review…".
- **Upload.** "Reading two-column.pdf…" with a "Profile import / Starting…" card. Then exactly one
  toast, "Read 2 roles, 1 schools and 0 skills.", and "Version 1". No further toast came across
  later polls.
- **Resume editor.** A typed headline raised "Unsaved changes to your resume."; "Save changes"
  toasted "Profile saved", and the section read "Version 2".
- **Settings.** Five ArrowLefts on the deep-review slider gave "Deep review the best 10 matches";
  "Save settings" toasted "Settings saved", and the SaveBar was gone.
- **Worker → Send a test job.** "Answered by process 78575 in under a second." That pid matches
  the worker log's `ready (pid 78575)`.
- **Worker stopped normally.**
  - SIGINT went to the dev worker's process group. There were 0 `procrastinate_workers` rows
    afterwards, and 2.5 s later the panel read "Offline / The worker is not running. Start it with
    pnpm dev:worker." and the sidebar "Worker offline".
  - That SIGINT also ended the log pipe. A later stop, with SIGINT to watchfiles only, logged
    `stopping (running jobs get 5.0s)` and then `stopped`.
- **Add company.** "linear.app" showed a "Company discovery / Starting…" card, then exactly one
  toast: "Linear was already on the list. Its Ashby board has 31 open roles.".
- **Refresh a row.** Linear → "Fetch now". The card row went "Board refresh / Refreshing job
  boards 0s / Stop", then "…1s", then "Linear: 31 open roles, no changes.". Exactly one toast
  carried that sentence.
  - The menu item is in a portal the accessibility tree did not list, so it was clicked with
    `.click()`. The menu itself opened with a real click.
- **Refresh stale boards.** The start toast came 64 ms after the click. 89 ms after the click the
  outcome "None of your boards are stale." replaced it in place.

**8B.**
- **Approve advances the run.**
  - Home's "Run a search" showed a toast and a scoring card.
  - Gate 1 came about 62 s later.
  - "Review 10 roles" was clicked on the `ApprovalCard`. The deep review ran, and gate 2,
    "Shortlist 4 roles", followed.
  - "Shortlist 4" completed the run. The start toast was replaced in place by "Found 600 roles,
    scored 600, deep-evaluated 10. 4 look worth applying to; 4 shortlisted.".
  - Both approvals are `approved` via `ui`.
- **Render.**
  - The Matches board read "457 open matches, 4 shortlisted and 0 applied".
  - The list had 457 rows.
  - Match detail rendered "New Grad Software Engineer, Product Engineering at Nuro – Foray" with
    the fake review and the posting.
  - Activity rendered the runs table and the event log.
- **Drag and drop.** A real pointer drag of the first Found card to Applied toasted "Moved to
  Applied … Undo", and the header read "1 applied". After a full reload the card was in Applied.
- **Stop shows the cancel message.**
  - With `LLM_FAKE_DELAY_MS=500` the next search reached gate 1 before Stop could be pressed.
  - The worker was restarted with `LLM_FAKE_DELAY_MS=5000`. Approving the deep review then gave
    "Reading 0 of 10 postings in full / Stop" on Home.
  - Stop toasted "Stopped. Everything found so far was kept.", and the card left "In progress".
  - The run is `cancelled`, and its last event is `run.cancelled`.

**8D.**
- **Streaming.** "What's on my resume?" from `/chat`: the address became `/chat/<id>`. The
  `POST /api/chat` response was the SSE stream: `start`, the `get_resume` tool chunks,
  `text-delta`, `finish` and `[DONE]`.
- **`users.timezone`.** It was `UTC` before the turn and `America/Los_Angeles` after it. That is
  the pane's `Intl` zone.
- **`list_jobs` card.** "Show me my matches" (§6's plan) rendered 10 match lines with
  `/matches/<id>` links, stages and scores, then "Here are your matches.".
- **Follow-up divider.** "add linear.app" rendered the "Company discovery" card. Then, without a
  reload (a window marker survived), the `role="note"` divider "Discovery finished", the
  follow-up's card and "Foray checked the run: it is completed…" appeared.
- **Recent chats.** The sidebar listed the chat. Options → "Delete chat" sent
  `DELETE /api/conversations/<id>` (200), the list emptied, the pane went to `/chat`, and
  0 conversations remained.

### 8.3 Light (`foray-theme=light`, reload; the key was removed afterwards)

- Screenshots were taken of these, and all rendered cleanly:
  - Home;
  - the Matches board and list;
  - match detail;
  - Activity;
  - Profile (with resume);
  - Settings, where Send a test job gave "Answered by process 79685";
  - Companies.
- **Add company.** Its card, then exactly one toast.
- **Chat.** It streamed to `/chat/<id>` and showed the `list_jobs` card, the run cards, the
  "Discovery finished" divider and the reply. It was then deleted from Recent chats.
- **U2, fresh, after signing U1 out with Settings' Sign out.** In light:
  - the no-resume Profile (dropzone and Preferences);
  - a Preferences save ("Preferences saved", SaveBar gone);
  - an upload, with its card going "Starting…" then "Picking out your roles, schools and
    skills", then exactly one counts toast and "Version 1".

### 8.4 Console and logs

- **Console.** Errors only: the one provoked 401 of §8.1.
- **API.** No 4xx or 5xx besides the 401s on `/api/me` while signed out.
- **Worker.** No warning or error.
- **Web.** One `⨯ Error: The destination stream closed early.`, right after "Search again"
  followed by a navigation within a second (integration-G4.md §4 saw the same).

### 8.5 Observations, no change

- **Each board refresh shows twice in the feeds** (integration-G4.md §4). It is still open, for
  step 10.
- **The list view's Role column.** At the pane's 1024 px width, Matches' list view squeezes the
  Role column to a few characters ("N…", "Se…"). Location and pay keep their width. It is 8B's
  layout, and no Done item covers it; step 10 should look at it.

## 9. Checks run in this step

- **pytest.** `FORAY_TEST_DB_NAME=test_foray_app_8e --create-db`, then a final rerun: 1706
  passed, with 1 warning (the third-party google-genai `DeprecationWarning`). The baseline was
  G4's 1701, plus 5 new tests. Under the simulated skew, +8 and −8 each gave 1706 passed (§7).
- **Static.**
  - `ruff check` and `ruff format --check` (271 files): clean;
  - pyright: 0 errors;
  - `next typegen`, then `tsc --noEmit`: exit 0;
  - `eslint`: exit 0;
  - `pnpm build`: exit 0, with all 13 routes.
- **Drift.** `pnpm api:types`, then `git diff --exit-code backend/openapi.json
  src/lib/api/schema.d.ts`: clean against the staged regeneration.
- **Money.** $0. The ledger's rows from this step are all `fake` (2014 rows, $0). JSearch's
  counter is unchanged (`2026-10 = 3`).
- **Data.**
  - `create_test_user --purge` printed "Deleted 2 test users.".
  - `foray_app` has 0 users, 0 sessions, 0 runs, 0 conversations, 0 `todo`/`doing` jobs and
    0 worker rows.
  - `public/e8-two-column.pdf` was deleted, and the pane's tab was closed.
- **Processes.** The worker logged `stopped`, uvicorn logged "Finished server process", and
  `next dev` exited. `:3000`, `:8000` and `:8001` are free.
- **Legacy `foray`.** The write counters were 41158/1014040/5351 before and after.
  `git status` of the main checkout was `?? reports/` and `?? research_notes/` before and after.
- **Test databases for the orchestrator to drop:** `test_foray_app_8e`, `test_foray_app_8e_skew`
  and `test_foray_app_8e_mut`.
