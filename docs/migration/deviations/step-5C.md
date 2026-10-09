# Step 5C: deviations and clarifications

Later steps read this before the design. Each entry: what, why, evidence.

Step 5C owns `backend/apps/jobs/{pipeline,tasks,api}.py` and `backend/tests/jobs/test_pipeline.py`.
The services live in `pipeline.py`; `tasks.py` registers the three tasks; `api.py` holds the four
routes.

## 1. The start functions, for the API and the chat tools (contract for step 6)

- **What.** Starting is sync and shared, in `apps.jobs.pipeline`. Each returns
  `Started(run, reused, message, queued=0)`:
  - `start_add(user_id, query, *, conversation_id=None, refresh_after=True)`;
  - `start_names(user_id, names, *, conversation_id=None)`, for chat's `discover_companies`;
  - `start_refresh(user_id, slug)`;
  - `start_refresh_stale(user_id)`.

  They raise `ApiError` with the user's sentence. Step 6's tool wrapper already returns an
  `ApiError`'s message (§7.2). The cases are:
  - 400 for an empty query, a query over 300 characters, or an empty name list;
  - 404 "That company is no longer on the list.";
  - 429 from the throttle.

  Chat's `add_company` calls `await db(pipeline.start_add, user_id, name_or_url,
  conversation_id=deps.conversation_id)` and returns `{ok: True, runId: started.run.id, message:
  started.message}`. `discover_companies` proposes names in the turn, then calls
  `pipeline.start_names(...)` and returns `queued` = the run's `input.names`.
- **Throttle.** `apps.core.throttle.throttle(f"company-add:{user}", COMPANY_ADD.limit,
  COMPANY_ADD.window_s)` (30 an hour) runs inside `start_add` and `start_names`, so the endpoint
  and both chat tools share one counter, as §4.5 says.
  - It runs after the query check, so malformed attempts are not counted (the same order as 3A's
    sign-up).
  - A name list counts once per call, not once per name. A 25-name discovery would otherwise
    take most of the hourly allowance.
  - `throttle.COMPANY_ADD` is a module constant (`Limit(30, 3600)`), not `Limit.COMPANY_ADD`.
- **Chat's `add_company` now fills API boards.** `refresh_after` defaults to `True` for every
  single query (§10.5 `refreshAfter`), so a company added in chat has its postings at once.
  Today's chat tool said "pulled in on the next refresh". Step 6 may pass
  `refresh_after=False` to keep today's behaviour.
- **Evidence.** `test_pipeline.py::test_the_31st_add_in_an_hour_is_throttled`: 30 adds answer
  202, the 31st answers 429 "Too many attempts. Try again in a few minutes.", and a
  `start_names` call after that is refused too. Another user is unaffected.
  `::test_add_refuses_empty_long_and_unreadable_queries_before_counting` shows no counter row
  after four refused bodies.

## 2. Wording and status codes the design left open

- **A second start while the same run is active** answers **202** with that run's id. It does
  not answer 409 as `POST /api/searches` does: the component then watches the same run. The
  dedupe keys and messages are:
  - `add:<query, trimmed, whitespace collapsed, lowercased>`: "Already looking for “<q>”.";
  - `add:names:<sha1 of the sorted normalised names>`: §9.4 names only the single-query key;
  - `refresh:<slug>`: "Already refreshing <name>.";
  - `refresh:stale`: "The stale boards are already being refreshed.".
- **The 202 messages.** Today's actions were synchronous and had no "started" sentence, so
  these are new:
  - an add: "Looking for a job board for “<q>”.";
  - one refresh: "Refreshing <name>.";
  - names: today's chat sentence, "Looking for job boards for N companies: A, B. Progress will
    appear in the activity feed.";
  - refresh-stale: today's `refreshAllAction` sentences, "Refreshing N stale boards in the
    background. The table updates when they are done.", or "Every active board was fetched in
    the last two hours, so none are stale." when nothing is stale.

  `queued` is the stale count when the run started.
- **`refresh-stale` always starts a run, even with nothing stale.** The run completes "None of
  your boards are stale." and emits `company.refresh.done` with zeros. This is bug #8's fix and
  the Done check: the button always gets a run to watch.
- **Run prompts** (`input.prompt`; only the search card shows a prompt) are:
  - "Add <q>";
  - "Find job boards for A and B";
  - "Refresh <name>";
  - "Refresh the stale boards".
- **Validation.** An empty or over-long query answers 400 with today's sentences ("Type a
  company name, its website or a job board link." / "That is too long to be a company name or a
  link."), as §5.2 treats field validation. A missing or non-string `query` gets the first of
  those. A `status` other than `active`/`paused` gets §5.2's default "That request could not be
  read.".
- **Outcomes that are not failures complete; they do not fail.**
  - An add with no board completes with `registered: false`, the trail and today's not-found
    sentence (URL or name variant), at level `warn`.
  - A single refresh that hits a 404, an error or an unsupported board also completes at level
    `warn`, with today's `refreshCompanyAction` sentence, e.g. "Gone: board not found, so it is
    marked invalid." `company.refresh.done` then has `ok: 0`.
  - Only an unexpected exception fails the run (`FAILURE_PREFIX`, "Could not add that
    company: …" / "The refresh failed: …").
- **The names run's completion sentence** is new; the TS job emitted only per-name events. It
  is "Registered A and B. No job board found for C." (either half alone when the other is
  empty). `added` holds the registered companies' names; `notFound` holds the names as given.
  Names run with `refresh_after=False`, as the TS `company.discover` job did, and with
  `discovered_via='agent'`; a single query uses `'user'`.
- **The stale-set message** stays the worker's, verbatim: "Refreshed <ok>/<n> boards — <postings>
  postings, <closed> closed" (no final period).
- **Four smaller additions** (recorded in fix round 1):
  - **An empty name list** answers 400 "Name at least one company to look for."
    (`NAMES_MISSING`; blank names are dropped first). Why: the TS tool never sent an empty list
    (it answered "Could not think of matching companies." itself), so the service needs its own
    sentence for a caller that does. Checked before the throttle, like the query checks.
  - **A run whose `input` does not read** fails with "This run's request could not be read."
    (`UNREADABLE_INPUT`), raised as `UserFacingError`, so it has no `FAILURE_PREFIX`. Why: only
    the start functions write these inputs, so this is a damaged row, not a failed fetch;
    "Could not add that company: …" would blame the company.
  - **A single refresh whose company was deleted after the start** completes at level `warn`
    with "That company is no longer on the list." (the start's 404 sentence), zero counts, and
    `company.refresh.done` first. Why: it is not a failure of the refresh (§2's "outcomes that
    are not failures complete"), and the button watching the run still gets its done event.
    Nothing in the app deletes companies today.
  - **`start_names` drops repeated names** (after trimming, first spelling kept): `["Linear",
    " Nowhere Labs ", "Linear"]` runs two lookups. Why: a second lookup would repeat every
    outbound request only to report the same board again, and the dedupe key is built from the
    names anyway.
- **Evidence.** `test_pipeline.py` pins every one of these strings. The four additions:
  `::test_names_refuse_an_empty_or_long_list_before_counting` (400s, no counter row, no run),
  `::test_a_run_whose_request_cannot_be_read_fails_with_a_sentence` (an add and a refresh, each
  `failed` with exactly that error and one `run.failed`),
  `::test_a_company_gone_before_its_refresh_runs_completes_with_a_warning` and
  `::test_names_register_each_board_and_report_the_rest` (the repeated name).

## 3. Events stay before the terminal event (an addition, for R4)

- **What.**
  - A run's progress events (`company.discovered`, `company.not-found`) are written only while
    the run is active, in a transaction that first takes the run's row lock (`SELECT … FOR
    UPDATE`).
  - The final pair (`company.refresh.done` or the not-found event, then `run.completed`) is
    written in one such transaction.
  - So after a Stop nothing lands after `run.cancelled`, and a names run stops before the next
    name.
  - The row lock comes before `emit()`'s advisory lock. `cancel()` takes them in the same order,
    so the two cannot deadlock.
- **Why.** `emit()` alone would happily write `company.discovered` after `run.cancelled`, and the
  run's last event would no longer be its terminal one.
- **Evidence.**
  - Progress events (`_emit_while_active`):
    `::test_a_stop_between_names_ends_the_run_with_its_cancel`. The events are exactly
    `["run.cancelled"]`, and the second name is never looked up. A mutation that skips the
    active check fails it (`['run.cancelled', 'company.discovered']`).
  - The final pair (`_finish`): two tests stop the run inside the step, just before its final
    pair is written.
    - `::test_a_stop_just_before_the_final_pair_leaves_the_cancel_last`: a single refresh whose
      `refresh_company` cancels the run, then returns an `ok` outcome.
    - `::test_a_stop_just_before_a_not_found_add_finishes_leaves_the_cancel_last`: an add whose
      `discover_company` cancels the run, then returns no board.

    Each ends `cancelled` with no output and events exactly `["run.cancelled"]`. With `_finish`'s
    `if not _lock_if_active(run_id): return False` reduced to `_lock_if_active(run_id)`, both
    fail (`['run.cancelled', 'company.refresh.done']` and `['run.cancelled',
    'company.not-found']`) and the other 28 pass.

## 4. `MULTI_BOARD_GATE` is read at call time

- **What.** `pipeline` reads `refresh.MULTI_BOARD_GATE` (the module attribute) each time.
  `test_pipeline.py` gives every test a fresh `asyncio.Lock`.
- **Why.** An `asyncio.Lock` binds to the event loop that first waits on it. The worker has one
  loop for its life, so production is unaffected. But each `run_deferred()` runs on a new loop,
  and a lock bound by one test would raise "bound to a different event loop" in the next.
- **Evidence.** `::test_a_stale_run_waits_while_the_cron_holds_the_gate`. The real cron task
  and a stale run share a 2-slot Worker. The run shows phase `waiting` while the cron fetches
  under the gate (`gate_held == [True]`). It then completes "None of your boards are stale.":
  - the board was fetched once, by the cron;
  - the cron made no run and no event.

  Two mutations each fail it with a timeout: the cron without the gate, and the stale run
  without it.

## 5. OpenAPI component names are prefixed

- **What.** The route schemas are `AddCompanyIn`, `CompanyRunOut`, `RefreshStaleRunOut`,
  `CompanyStatusIn`, `CompanyStatusOut` and `CompanyRow` (in `pipeline.py`, so step 7's
  companies page can import it).
- **Why.** ninja 1.7.1 merges every operation's schemas into `components.schemas` with a plain
  `dict.update` (`ninja/openapi/schema.py:336-342`, "TODO: check if schema definitions are
  unique"). Two lanes that both define, say, `StartedOut` with different fields would
  silently get one of them in `openapi.json`. **For the integration:** check `openapi.json` for
  generic names (`StartedOut`, `StatusIn`, …) defined in more than one module.
- **Also.** The 202 routes return `ninja.Status(202, …)`. A `(202, body)` tuple works but logs
  ninja's `DeprecationWarning` on every call.

## 6. Kill during a run: proved in pytest; the live check is deferred

- **What.** The live check (`kill -9` the worker while a stale-set run is `refreshing`, restart,
  "Interrupted…" within about 2 minutes) needs the worker process, which this group may not
  start. Two tests prove the same path instead.
  - `::test_a_worker_stopped_mid_refresh_leaves_the_run_to_the_reaper`
    1. A real in-process Worker is stopped while the run's board fetch hangs in phase
       `refreshing`.
    2. Its job ends with no `todo`/`doing` job holding `run:<id>`, and the run stays `running`
       (`CancelledError` is not caught, §9.2).
    3. After the 60 s margin, `reap_runs()` fails the run with `reaper.INTERRUPTED`, and its last
       event is `run.failed`.
  - `::test_a_killed_worker_leaves_a_stalled_job_that_the_restart_clears`. This is the state
    `kill -9` leaves: the run is `running` in phase `refreshing`, and its job is `doing` on a
    worker whose heartbeat is 300 s old.
    1. The reaper spares the run, because its job still holds it.
    2. The restart's startup sweep (`fail_stalled_jobs()` under the worker connector) fails
       exactly that job.
    3. The next reap fails the run "Interrupted…" with one `run.failed` event.

## 7. Observed, not changed: pasting a board link renames the company (handoff, 3C's file)

- **What.** `discover_company("https://jobs.ashbyhq.com/linear")` registers the board with
  `name = title_case(raw)`, i.e. "Https://jobs.ashbyhq.com/linear", and `domain =
  "jobs.ashbyhq.com"`. On an existing company the upsert overwrites both: `name =
  EXCLUDED.name`, `domain = coalesce(EXCLUDED.domain, …)`. So pasting the board link of a seeded
  company renames it to its URL. The add then completes "Added Https://jobs.ashbyhq.com/linear.
  Its Ashby board has 4 open roles.".
- **Why not fixed.** `apps/jobs/discovery.py` is step 3C's file, and TypeScript behaves exactly
  the same (`src/lib/companies/discovery.ts` `finish()`: `name: titleCase(name)` with
  `name = raw`, `domain = normalizeDomain(raw)`).
- **Suggested fix.** In `finish()`, when the match came from a pasted ATS link (or the name
  still looks like a URL), name the company after the ATS slug (`title_case(slug)`) with no
  domain. Also, do not overwrite an existing company's name with such a guess.
- **Evidence.** A temporary print in `::test_a_pasted_board_link_is_verified_and_filled`,
  since removed, showed `Https://jobs.ashbyhq.com/linear jobs.ashbyhq.com linear`. The tests
  therefore assert the trail and the counts there, not the name.

## 8. Checks run in this lane

- `FORAY_TEST_DB_NAME=test_foray_app_lane5c uv run pytest` (after fix round 1):
  - 1333 passed, 7 skipped, 10 xfailed;
  - the skips and xfails are the existing pending register rows, the chat tools, the profile
    page and the strict jsonio xfails;
  - `tests/jobs/test_pipeline.py` alone: 30 passed in 3.0 s.
- `ruff check`, `ruff format --check` (232 files) and `pyright` (0 errors) are clean.
- `manage.py export_openapi_schema` into a scratch file lists `POST /api/companies` (202),
  `POST /api/companies/refresh-stale` (202), `PATCH /api/companies/{slug}` (200) and `POST
  /api/companies/{slug}/refresh` (202). The committed `openapi.json` is untouched (§5.4).
- No real model was called ($0). Legacy `foray` write counters stayed at 41158/1014040/5351.
- **Deferred to the integration** (they need `:3000`/`:8000` or a worker process; §15.2):
  - the three `curl` checks through `:3000`;
  - the live `kill -9`.

  Their pytest equivalents are listed in the step report.
