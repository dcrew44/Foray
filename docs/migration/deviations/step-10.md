# Step 10: deviations and clarifications

Later steps read this before the design. Each entry: what, why, evidence.

Step 10 owns "fixes only" (§15.2). It ran §15.3 items 2–15 in the browser pane twice for
throwaway users: on `LLM_FAKE=1` with `JSEARCH_ENABLED=false` ($0), then once on real models
($0.347613 by the ledger). It then ran `manage.py acceptance_checks`. Six fixes came out of it, each
with its owning step's tests re-run (§9). No migration was added and nothing was committed.

Files changed:

- **backend:** `apps/chat/fakes.py`, `apps/jobs/discovery.py`, `apps/jobs/pipeline.py`,
  `apps/runs/management/commands/acceptance_checks.py`;
- **backend tests:** `tests/chat/test_chat_view.py`, `tests/jobs/test_discovery.py`,
  `tests/jobs/test_pipeline.py`, `tests/runs/test_commands.py`;
- **frontend:** `src/components/companies/add-company.tsx`,
  `src/components/matches/match-table.tsx`, `src/components/runs/run-card.tsx`.

## 1. Fix: the chat fake can run the interview (item 2, step 6's `chat/fakes.py`)

- **What.** Two plans in the `chat` fake, ahead of "find"/"search":
  - **"set up"** (Home's "Answer in chat" sends "Let's set up my job search. Ask me what you need
    to know."): `get_profile_summary`, then the interview's question.
  - **Labelled answers**, `<label>: <value>` separated by `;` or new lines, list values separated by
    commas (`Roles: Software Engineer, Backend Engineer; Level: entry; Locations: San Francisco Bay
    Area; Remote: hybrid; Work authorisation: US citizen`): the real `update_profile_preferences`
    with those fields and every other argument null, then "Saved. Foray has what it needs to
    search." or "Saved. Foray still needs your …". A level or remote value that is not one of the
    options is left out, never guessed.
- **Why.** Item 2 expects "Answering produces `update_profile_preferences` lines" on fakes. No fake
  plan called that tool. Worse, Home's opener contains "search", so the fake ran
  `start_search_run` (refused for missing preferences) instead of the interview.
- **Evidence.**
  - `tests/chat/test_chat_view.py::test_the_chat_fake_runs_the_interview` checks the opener (one
    `get_profile_summary` call, the question, no run) and the labelled answers (the exact tool
    input, `blockingSearch == []`, the stored preferences, the reply, and the Node contract).
  - In the pane (fakes): the opener answered with the question. The answers showed "Saved your
    target roles, seniority, locations, remote preference and work authorisation. Ready to
    search.", and Profile → Preferences held every value.

## 2. Fix: web-search results that are about something else are skipped (item 9, step 3C's `discovery.py`)

- **What.** In discovery's search step, a Brave result is used only when it is about the company
  (`discovery.is_about`):
  - its URL holds one of the name's first slug guesses: the joined name always, other guesses only
    when they are 4 characters or longer, so "the" in "The Browser Company" does not count;
  - or its page names the company, ignoring case, spacing and HTML entities.

  A skipped result adds the trail step `search-skip` with the URL as its detail. Add company labels
  that step "Search result about something else".
- **Why.** With a real `SEARCH_API_KEY`, `notarealcompany12345` got 5 loose results. The careers-page
  fallback then registered "Notarealcompany12345" as a careers-page company (needs-browser) with
  three strangers' pages: `notarealcompany.hire.trakstar.com`, a notaries.com article and
  `notarycam.com/careers`. Item 9 expects "the trail and 'No job board found'". The TypeScript had
  the same behaviour (`discovery.ts` pushed every loadable result). A result page that links an ATS
  board would also have given the made-up name a stranger's board.
- **Cleanup.** The false-positive row was deleted from `foray_app`; it had no jobs.
- **Evidence.**
  - `tests/jobs/test_discovery.py::test_search_results_about_something_else_are_skipped` uses the
    three real URLs. Nothing is registered, the trail is `slug-guess, search, search-skip ×3`, and
    no ATS host is probed.
  - `::test_a_search_result_that_names_the_company_is_used` covers a page that names the company
    ("Quiet Lantern &amp; Co").
  - The existing `test_search_finds_a_careers_page` passes unchanged: `obscure` is in
    `obscure.io`.
  - Mutation: with the check disabled, the skip test fails. Restored, all 54 discovery tests pass.
  - Pane, both passes: "No job board found for “notarealcompany12345”." with the trail (guessed
    names, 5 search results, three "Search result about something else") and exactly one toast.

## 3. Fix: a board refresh shows once in the feeds (integration-G4.md §4, step-8E.md §8.5)

- **What.** `jobs/pipeline._complete_refresh` emits `company.refresh.done` at level `debug`.
  `run.completed` carries the same sentence, at `info` or `warn`. The catalogue's `DEFAULT_LEVEL`
  for the kind stays `info`: `tests/test_models.py` pins it to the legacy worker map
  (`map/worker.md` §19), and the pipeline passes its level explicitly. A first attempt that also
  changed the catalogue failed that test and was reverted.
- **Why.** Home's "Latest activity" and Activity's event log skip `debug` events unless the debug
  switch is on, so each refresh no longer prints its sentence twice. The companies page still
  re-reads on it, because `/api/live` serves every level and `RefreshOn` does not filter by level.
- **Evidence.**
  - Before, Activity's log listed "Refreshed 22/22 boards — 4884 postings, 2 closed" and "Linear:
    31 open roles, no changes." twice each.
  - `tests/jobs/test_pipeline.py` now pins the `debug` level on the ok, failed and gone paths
    (three asserts updated).
  - `tests/jobs` and `tests/runs`: 603 passed.

## 4. Fix: `acceptance_checks` tells a deleted chat from a missing link (step 4's command)

- **What.** The attribution line has two parts:
  - a non-chat row with a user but no run fails, as before;
  - a chat row with neither `conversation_id` nor `run_id` fails only when one of the user's chats
    that still exist was open at that moment (`created_at − 2 s ≤ row ≤ updated_at + 2 s`).
    Otherwise it belonged to a chat deleted since, and the line reports "(N row(s) of deleted
    chats)" without failing.

  A chat row with a run (a follow-up's) is attributed.
- **Why.** Deleting a chat sets its ledger rows' `conversation_id` to NULL (`LlmCall.conversation`
  is `SET_NULL`). Item 13 deletes a chat before item 16 runs these checks. As written, the line
  failed on every row of that chat: 18 rows on the fake pass. Keeping the link through deletion
  would need a migration (`db_constraint=False`), more than a "fixes only" step should carry.
  `conversations.updated_at` moves at every turn start and finish (`chat/models.py`), so a turn's
  rows fall inside its chat's window. The 2 s slack covers the mixed clocks of step-8E.md §7.7.
- **Evidence.**
  - `tests/runs/test_commands.py::test_acceptance_checks_tell_a_deleted_chat_from_a_missing_link`:
    a deleted chat's turn row and follow-up row pass, reported as "(1 row(s) of deleted chats)". A
    chat row with no link while another chat is open fails.
  - The other four `acceptance_checks` tests pass unchanged.
  - Mutation: the old line fails the new test.

## 5. Fix: the run card names the `shortlisting` phase (step 8B's `run-card.tsx`)

- **What.** `describe()` returns "Shortlisting the roles worth applying to" for phase
  `shortlisting`. It used to fall through to "Working…".
- **Why.** §9.4/§14 D4 now write the phase, and the "what changes" table promises "The search card
  shows the 'Shortlisting' phase".
- **Observed (not a defect).** In approve-each the phase lasts milliseconds: the continuation
  resumes the run with `phase=shortlisting`, moves the matches (no model call) and completes. An
  in-page poll of `/api/live` every 30 ms during the real pass saw `awaiting_approval/
  awaiting_shortlist` → `running/awaiting_shortlist` (the decision) → `completed/done` 43 ms
  later, so item 4's "Shortlisting shown on the way" cannot be seen at the 1.5 s cadence. The
  step list's "Shortlist" step stays lit through both phases, and on autopilot gate 2 the phase
  lasts until the continuation runs.

## 6. Fix: the match list's Role column at 1,024–1,279 px (step 8B's `match-table.tsx`)

- **What.** Location shows from `xl` (1,280 px) instead of `lg`.
- **Why.** Step-8E.md §8.5 left it to this step. The table is `table-fixed` with 642 px of fixed
  columns, so at the pane's 1,024 px Role got about 70 px and read "So…". The layout is legacy
  (unchanged since main); this is the cheapest readable fix.
- **Evidence.** In the pane, Role went from about 70 px to 252 px. Fit, role, pay, stage and posted
  stay visible, and the fit ≥ 70 filter is unaffected.

## 7. How the Done check ran (deviations from §15.3's setup)

- **Servers.** `pnpm dev`, `pnpm dev:api` and `pnpm dev:worker` ran as three separate background
  process groups, each logging through a line timestamper (it ignores SIGINT, so "stopped" reaches
  the log). They did not run under `preview_start foray-web`, because items 11, 13 and 15 restart
  the worker or the api with their own environment (`LLM_FAKE_DELAY_MS`, `LLM_FAKE_FAIL`,
  `LLM_MONTHLY_BUDGET_USD`). The pane was opened with `preview_start {url}`. `.env` was never
  edited; every override was in a process environment.
- **Fake pass:** `LLM_FAKE=1 JSEARCH_ENABLED=false`, user `s10-fake@example.test` (fresh, no resume).
- **Real pass:** no `LLM_FAKE`, JSearch at its `.env` setting (197 of 200 left; one search uses at
  most 3; the step used 5). `LLM_MONTHLY_BUDGET_USD=1.67` (month-to-date $0.270806 + $1.40) was the
  backstop until item 15. User `s10-real@example.test` (fresh); its `deepEvalTopN` was set to 5
  before the search, to bound evaluate cost.
- **The pane was hidden throughout.**
  - The checks dispatched `foray:live-refresh` every 1.5 s, as G4 and 8E did.
  - Toasts were counted with a `MutationObserver` on `[data-sonner-toast]`.
  - "Without a reload" was proven by a `window` marker surviving.
  - Uploads used the dropzone's own file input with a `File` fetched from
    `public/s10-two-column.pdf` (deleted afterwards).
- **Labels that differ from §15.3's words.** Home's "Set up my search" is "Answer in chat" (the
  legacy label too). Evaluate is "Get a deep review". Render PDF is "Make a PDF". Worker → Test is
  "Send a test job".

## 8. Items that could not be shown as written, and what stood in

- **Item 7, the preview iframe.** The pane's browser refuses the sandboxed iframe's own load
  (`net::ERR_BLOCKED_BY_CLIENT` in `read_network_requests`; the same URL fetched by script answers
  200 with the CSP header), so the pane shows a white box. A headless Chrome page loaded the match
  page with the throwaway cookie (scratch `preview_check.py`, Playwright with local Chrome):
  - on fakes and on real models, the iframe rendered the resume;
  - the frame answered 200, and there were no console errors and no CSP messages.

  The PDF served `inline` and, with `?download=1`, `attachment`, `application/pdf`, starting
  `%PDF-`.
- **Item 4, "Activity shows 'N strong'".** `runOutcome` (unchanged since legacy, §6.8) prints "N
  shortlisted" when anything was shortlisted, and "N strong" only otherwise. §14 D4 fixed the dead
  value in the backend (`output.strong` is written), not the rule. So item 4's run, which
  shortlisted, reads "306 scored, 3 shortlisted". "N strong" shows for searches that shortlisted
  nothing: "0 scored, 82 strong" and "12 scored, 4 strong" on fakes, both skipped at gate 1.
- **Item 11's forced failure and item 13's forced chat failure** use `LLM_FAKE_FAIL`, which exists
  only on fakes. They ran on the fake pass. On the real pass:
  - the kill-9 interrupt ran on a real tailor;
  - the failed chat turn was produced by the budget guard ($0.01), and "Try again" after restoring
    the default left exactly one user message and one reply.
- **Item 14's poll cadence** depends on `document.hidden`, and the pane was hidden (6 s busy and
  30 s idle there).
  - Naturally hidden and idle, the polls came at +0.2 s and +30.2 s.
  - With `document.hidden` overridden to `false` and `visibilitychange` dispatched, the real
    provider polled every 6.02 s idle and every 1.52 s busy (a search started through
    `POST /api/searches`, then stopped). The timing came from a `window.fetch` wrapper.
- **Item 10's light switch** used a real click on the fake pass and a script click on the real
  pass. The theme key was removed afterwards (it was unset before).

## 9. Checks run in this step

- **pytest**, `FORAY_REGISTER_STRICT=1` (step-1.md §12), `FORAY_TEST_DB_NAME=test_foray_app_step10`:
  - baseline: 1706 passed;
  - after the fixes: 1710 passed (the 1706 plus 4 new tests: the interview fake, two discovery
    tests and the deleted-chat attribution test). No register row is pending.
- **Owning steps' tests re-run after each fix:**
  - `tests/chat/test_chat_view.py -k fake` (2 passed);
  - `tests/jobs/test_discovery.py` (54 passed);
  - `tests/jobs` + `tests/runs` (603 passed);
  - `tests/runs/test_commands.py` (10 passed).
- **Static.** `ruff check`, `ruff format --check` (271 files) and pyright (0 errors) are clean.
  `next typegen`, then `tsc --noEmit`, exit 0. `eslint` exit 0. `pnpm build` exit 0 with 13 routes.
  `pnpm api:types`, then `git diff --exit-code backend/openapi.json src/lib/api/schema.d.ts`: no
  drift.
- **Mutations** (each fix undone, its tests run, then restored):
  - the discovery check off: 1 failed;
  - the old attribution rule: 1 failed;
  - no interview plans in the chat fake: 1 failed;
  - `company.refresh.done` back at the run's level: 3 failed.
- **`acceptance_checks`**, worker running:
  - after the fake pass, `--since` its start: 8 PASS, exit 0;
  - after the real pass, `--since` the step's start and without `--since`: 8 PASS, exit 0. The
    attribution line read "all attributed (16 row(s) of deleted chats)", and month-to-date was
    "$0.618419 of $90".
- **Money.**
  - The fake pass added 434 ledger rows at $0.000000.
  - The real pass added 400 rows, $0.347613: score 376 ($0.207505), evaluate 5 ($0.100002), tailor
    1 ($0.031652), chat 16 ($0.006299), extract 1 ($0.001695), verify 1 ($0.000460). The ceiling
    was $1.50.
  - The killed real tailor's in-flight call has no row: a SIGKILLed process writes nothing (R10
    holds for every request the process survives).
- **JSearch.** `api_usage` `jsearch 2026-10` went from 3 to 8 (the real pass's two searches).
- **Data afterwards.**
  - `create_test_user --purge` printed "Deleted 2 test users.".
  - `foray_app` has 0 users, sessions, runs, conversations, documents and worker rows, and no
    todo/doing jobs.
  - PostHog (`ashby:posthog`, added on the fake pass's new-company path) stays in the dev registry.
- **Processes and legacy.** Every server and worker was stopped, and :3000, :8000 and :8001 are free.
  The pane's tab was closed. The legacy `foray` write counters stayed at 41158/1014040/5351.
  `git --no-optional-locks -C <main checkout> status --porcelain` is `?? reports/` and
  `?? research_notes/`.
- **Test database for the orchestrator to drop:** `test_foray_app_step10`.

## 10. Observations, no change

- **The real model guesses approval ids.** On "yes, shortlist them" its first `answer_approval`
  used an id that does not exist, which shows the line "No approval with that id. Call
  list_pending_approvals for the current ones.". It then listed and answered correctly. The tool's
  refusal is doing its job; the prompt could name the pending approval in the follow-up notice.
- **A second SIGINT after "stopped".** With a group-wide SIGINT, watchfiles forwards its own SIGINT,
  which can land in `asyncio.Runner.close()` after the worker logged `stopped`. The result is a
  `KeyboardInterrupt` traceback; it was seen once in about 15 stops. Shutdown had already
  finished. This is dev-only (step-H1.md §1).
- **The pane's live poll once stopped polling** on one page load, right after I overrode
  `document.hidden` on a freshly loaded Home. No `/api/live` request followed for over 65 s, even on
  `foray:live-refresh`. A fresh load polled normally, and the stall did not recur in either pass. No
  console error was logged, and a direct `fetch('/api/live')` answered in 54 ms. The cause is not
  known; it may come from the override itself.
- **The Activity run list hides its cost column at 1,024 px.** The expanded run shows "Cost $0.02"
  (the ledger has $0.023294).

## 11. Independent verification (rerun from a clean state)

The verifier reran every Done check with its own processes and throwaway users
(`s10v-fake@example.test`, `s10v-real@example.test`), and reached the same results:

- **pytest**, `FORAY_REGISTER_STRICT=1`, `FORAY_TEST_DB_NAME=test_foray_app_step10v --create-db`:
  1710 passed, no skips. Each fix undone in turn fails its tests (discovery 1, refresh level 3,
  interview fake 1, attribution 1); the files were restored byte for byte.
- **Static:** ruff check, ruff format --check (271 files), pyright (0 errors), `next typegen` +
  `tsc --noEmit`, eslint and `pnpm build` (13 routes) are clean; `pnpm api:types` shows no drift.
- **Fake pass** ($0, 433 `fake` rows, JSearch unchanged at 8): items 2–15 pass. A new company
  (`resend.com`, Ashby, 14 roles) covered item 9's new path; it stays in the dev registry beside
  PostHog. Item 12 stopped a search at "Scoring 40 of 316" (16 in-flight calls ended
  `cancelled (input estimated)` within 40 ms, nothing later).
- **Real pass:** ledger delta $0.382161 (score $0.2118, evaluate $0.1036, tailor $0.0533, chat
  $0.0112, extract $0.0017, verify $0.0006); JSearch 8 → 17 of 200. Items 2–15 pass, item 11's
  kill -9 ending "Interrupted…" 101 s after the kill with one toast, item 12 stopping at
  "Scoring 40 of 79". The real model again guessed an approval id before listing (§10), and
  shortlisted 2 of the 3 offered on "yes, shortlist them".
- **`acceptance_checks`**, worker running, with `--since` the fake pass's start and without it:
  8 PASS, exit 0 ("all attributed (12 row(s) of deleted chats)", "$1.00058 of $90").
- **Pane notes.** With the pane hidden, the page has no layout (`innerWidth` 0); `resize_window`
  (1024×800, then 1400×900, reset afterwards) gives it one, so the board drag ran as synthetic
  pointer events. Base UI menus in Recent chats open from the keyboard (`ArrowDown` on the
  focused trigger), not from synthetic mouse events.
- **Observed, legacy behaviour, not changed:** after a tailor run fails, the panel keeps the
  failed run card and offers no "Tailor my resume" until a reload; and a chat turn that failed
  shows "Try again" only until the page reloads (the error lives in client state).
