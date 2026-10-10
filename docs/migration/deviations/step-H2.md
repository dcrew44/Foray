# Step H2: pre-cutover sweep — deviations and clarifications

Later steps read this before the design. Each entry: what, why, evidence.

Step H2 is not in §15.2. Its scope was the review findings F1–F18 and eight open items. Every
finding of severity medium or higher is fixed. The low ones are fixed where the fix was cheap;
F14 is fixed only in part, and §19 says why. Each fix has a test that fails without it. Every
fix was undone in turn and its tests run (§20). Seven migrations were added, all named
`00NN_h2_database_clock` (§17).

Where an entry changes what the design says, it overrides the design. The design text was also
corrected in these places:
- §2.1 and §2.2 (timestamps);
- §5.3, the `/sign-in` row (`safeNext`, round 2);
- §7.2 (untrusted text);
- §13.3 (compose);
- step 13 (`.env.legacy`, `STORAGE_DIR`, `SIGNUP_OPEN=false`, the `db:up` restart);
- §15.3 (which acceptance items run as the real user).

## 1. F1 / F18: the sign-in `next` guard refuses backslash and control-character paths

- **What.**
  - `safeNext` now lives in `src/lib/safe-next.ts` and is pure, so the page (server) and the
    form (client) share it.
  - It refuses a `next` that:
    - is not a string (a repeated `?next=` arrives as an array);
    - does not start with `/`, or starts with `//`;
    - holds a backslash, an ASCII control character, a space or DEL.
  - It then resolves the path against a fixed origin (`http://foray.invalid`) and keeps the
    result only if the origin is unchanged.
  - The resolved `pathname + search + hash` must then pass the same input check again: one
    leading `/`, not `//`, and no backslash, control character, space or DEL. Anything else
    goes to `/home`.
  - `sign-in-form.tsx` checks again before `router.push`.
- **Why.**
  - Browsers read `\` as `/` and drop tabs and newlines. So `/\evil.example` and
    `/<TAB>/evil.example` passed the old `startsWith("/") && !startsWith("//")` check. Yet both
    were scheme-relative links to another site: an open redirect straight after the password
    form.
  - Resolving collapses dot segments, and that can create a leading `//`.
    - Examples: `/.//evil.example`, `/a/..//evil.example/login`, `/%2e%2e//evil.example` and
      `/.%2e//evil.example` each resolve to the path `//evil.example`.
    - Typed as is, each of them stays on the site. The resolved path does not.
    - Round 1 returned the resolved path unchecked, so a signed-in visit got `307 Location:
      //evil.example`. This was a new open redirect: the old guard had passed these values
      through verbatim, and the browser had resolved them on the site.
    - Verification round 1 caught it, and round 2 added the check on the output.
- **Evidence.**
  - `src/test/unit/safe-next.test.ts` (5 tests):
    - The WHATWG parser takes each backslash or tab value to `evil.example`, and `safeNext`
      sends it to `/home`.
    - For each dot-segment value, the test shows two things. Typed as is, the value stays on
      the site. Its resolved pathname leaves it. `safeNext` sends it to `/home`.
    - A property check runs every listed value plus all 1,000 paths `/a/b/c`. Each segment is
      one of `""`, `.`, `..`, `%2e`, `%2E%2e`, `.%2e`, `a`, `evil.example`, `\` and `%5C`. The
      check asserts that the result has exactly one leading `/` and that
      `new URL(result, "http://site.example").origin` is that site.
    - Real paths, queries and fragments are kept: `/a/../b` becomes `/b`, `/./matches` becomes
      `/matches`, and `/a//b` stays `/a//b`.
  - With the round-1 function (no output check), 2 of the 5 fail: the dot-segment test and the
    property check. With the pre-H2 function, the backslash test fails as well.
  - Live (round 2), through `next dev` and `dev:api`, using a throwaway `@example.test` user's
    dev-session cookie and `curl -D -`:
    - These values each answered `307 location: /home`: `/sign-in?next=/.//evil.example`,
      `/a/..//evil.example/login`, `/%2e%2e//evil.example`, `/%2E//evil.example`,
      `/.%2e//evil.example`, `/%5Cevil.example/login`, `/%09/evil.example` and
      `//evil.example`.
    - `next=/matches%3Fview%3Dlist` answered `/matches?view=list`. `/./matches` and
      `/a/../matches` answered `/matches`.
  - In the browser pane (round 2): navigating to the verifier's
    `/sign-in?next=/.//127.0.0.1:8000/api/health` ended on `http://127.0.0.1:3000/home`.
    `fetch` of the `/a/..//…` and `%2e%2e` variants also ended at `/home`.

## 2. F2: Postgres on loopback only; `next dev` on 127.0.0.1; `SIGNUP_OPEN=false` after the import

- **What.**
  - `docker-compose.yml`:
    - `db` publishes `127.0.0.1:5432:5432`.
    - `POSTGRES_PASSWORD` is `${POSTGRES_PASSWORD:-foray}`, and the containers' `DATABASE_URL`
      uses the same value.
    - The header comment no longer claims that only web publishes a port.
  - `package.json`: `"dev": "next dev -H 127.0.0.1"`. Next's dev server still allows
    `localhost` as an origin (its `allowedDevOrigins` doc), so the pane and curl on
    `localhost:3000` work.
  - `.env.example` documents `POSTGRES_PASSWORD` and says to set `SIGNUP_OPEN=false` once
    the owner's account exists.
  - Design step 13 now sets `SIGNUP_OPEN=false` in the main checkout's `.env`.
- **Not changed, on purpose.**
  - The default password stays `foray`. Changing it only affects a fresh volume, and the dev
    `.env`'s `DATABASE_URL` carries it. The loopback binding is what closes the hole.
  - `web` in the prod profile still publishes `3000:3000`: that is the deployment's port, and
    it is behind TLS in Phase 4.
- **Handoff (step 13 / the operator).**
  - The next `pnpm db:up` recreates `foray-db-1` on the same volume. The binding and the
    restart policy (§9) changed, so the server restarts once.
  - This step did not run it, so `foray-db-1` still listens on `*:5432` until then.
- **Evidence.**
  - `backend/tests/test_repo.py`:
    - `::test_postgres_is_published_on_loopback_only`;
    - `::test_dev_servers_listen_on_loopback_only`.

    Both fail with the old `docker-compose.yml` or `package.json` (§20).
  - `docker compose -p foray-h2check --profile prod config` gives `db [('127.0.0.1', '5432',
    5432)]` and `web [(None, '3000', 3000)]`.
  - Live: `lsof` showed `node … 127.0.0.1:3000 (LISTEN)` and uvicorn on `127.0.0.1:8000`.
    `curl http://localhost:3000/api/health` answered 200, and `curl http://10.0.4.47:3000/…`
    (this machine's LAN address) was refused.

## 3. F3: a caller's headers never follow a redirect to another origin

- **What.** In `apps/core/http.py`, `fetch_text` keeps the User-Agent and Accept apart from the
  caller's headers. `_attempt` sends the caller's headers only while the hop's WHATWG origin
  (scheme, host, port) equals the first URL's. A hop elsewhere gets User-Agent and Accept alone.
  An http→https hop on the same host counts as another origin (strict).
- **Why.** JSearch's `x-rapidapi-key` and Brave's `X-Subscription-Token` went to whatever host a
  30x pointed at. The client follows redirects itself (`follow_redirects=False`), so httpx2's own
  `Authorization` stripping never ran. This is not a regression from main (Node's fetch did the
  same for these two headers), but a paid key should not leave its API.
- **Evidence.** `tests/jobs/test_http.py::test_a_callers_headers_never_follow_a_redirect_to_another_origin`.
  - It follows `jsearch.p.rapidapi.com/search` → `/search-v2` (same origin: the key and
    `authorization` are kept) → `https://collector.example.net/x` →
    `http://jsearch.p.rapidapi.com/back`.
  - The last two hops get neither header.
  - It fails without the fix.

## 4. F4: step 13's acceptance runs the writing items with a throwaway user

- **What.** Design §15.3's setup now says which user each item uses:
  - **Real user:** items 1, 5, 9, 10, 13, 14, 16 and 17. These are read-only, or their changes
    are put back.
  - **Throwaway user** (`create_test_user --with-resume --with-match`): items 2, 3, 4, 6, 7, 8,
    11 and 12.

  An operator who still wants one of those on the real user first records the touched
  `job_matches` columns (`id`, `stage`, `stage_changed_at`, `deep_score`, `deep_eval`,
  `notes`), then restores them, as item 10 does for the threshold.
- **Why.** Item 4's shortlist, item 6's "Review again" (`save_deep_eval` updates in place, and
  a skip verdict archives a `found` match), item 12's search and item 11's killed tailor all
  leave changes on the real board, and none of them were put back. The verifier rated it low:
  the data is recoverable from the legacy database and the backup. It is a procedure change, so
  nothing could be tested.
- **Evidence.** The design text (step 13's scope points at §15.3's rules).

## 5. F5: every `.env.*` but the template is git-ignored

- **What.** `.gitignore` replaces `.env.local` and `.env.*.local` with `.env.*` plus
  `!.env.example`. It adds `/storage.*/`, the folder step 12 moves the store to
  (`storage.pre-cutover-<stamp>`).
- **Why.** Step 13 copies the main checkout's `.env`, which holds live keys, to `.env.legacy`,
  and a `git add -A` would have committed it.
- **Evidence.**
  - `tests/test_repo.py::test_git_ignores_every_env_file_but_the_template` runs `git
    check-ignore --no-index`:
    - ignored: `.env`, `.env.legacy`, `.env.local`, `.env.production.local`, `.env.backup`,
      `storage.pre-cutover-20261009/…`, `storage/…`, `backups/…`;
    - not ignored: `.env.example`.
  - It fails with the old `.gitignore`.

## 6. F6: boot refuses a `STORAGE_DIR` that overlaps `LEGACY_STORAGE_DIR`

- **What.** `foray/env.py` `boot_checks` resolves both paths, following symlinks and creating
  nothing. It refuses a store that is the legacy tree, sits inside it, or contains it. The
  api, the worker and every `manage.py` command then fail at boot with one line naming
  `STORAGE_DIR`.
- **Why.** The main checkout's `.env` has `STORAGE_DIR=./data`, which after the merge resolves
  to the legacy `data/` folder. Only the importer refused that, so a half-done step 13 switch
  would have written new uploads into the legacy tree, unnoticed.
- **Evidence.**
  - `tests/core/test_guards.py::test_env_keeps_storage_out_of_the_legacy_tree`, five cases:
    - refused: `./data`, `./data/new`, `.`;
    - allowed: `./storage`, and `./data-new` (a sibling, not a prefix).
  - `::test_env_resolves_symlinks_before_comparing_stores` covers a store that is a symlink to
    the legacy folder.
  - Four of the six fail without the check.

## 7. F7: the README's import procedure ends with `mark_cutover`, and the refusal says what it would discard

- **What.**
  - **The refusal.** The non-empty-target refusal now reads: "`<db>` already has data (rows in
    …). Importing again discards everything in `<db>`, including whatever was created since the
    last import. Only if that is what you want: pass --replace, or run reset_app_db. If this
    database holds the data you use, run manage.py mark_cutover (pnpm db:mark-cutover) instead;
    both then refuse."
  - **The reminder.** A real import (not `--rehearsal`) ends with `NEXT_STEPS`: verify, then
    `pnpm db:mark-cutover`, and a note that until then `--replace`/`reset_app_db` can still
    discard the data.
  - **New script.** `pnpm db:mark-cutover` runs `manage.py mark_cutover`.
  - **Docs.** README's "Bringing over data" adds `--password-prompt` and `db:mark-cutover`,
    and says why to mark the cutover before using the app. CLAUDE.md's commands table names
    the script.
- **Why.** README users could run the import and leave both destructive commands armed against
  the real `foray_app`. The old refusal suggested both of them with no warning.
- **Evidence.**
  - `tests/legacy/test_import.py::test_refuses_a_non_empty_target` pins the new message: the
    discard warning comes before `--replace`, and `mark_cutover` is named.
  - `::test_import_command_reports_counts_checks_and_conversions` checks that the output ends
    with `NEXT_STEPS`, and the new `::test_a_rehearsal_import_does_not_ask_for_the_cutover`
    checks that a rehearsal does not.
  - Each fails without its change.

## 8. F8: a cancel during the document write waits for the write before judging the file

- **What.**
  - `apps/resumes/render.py` has a new helper, `_settled(path, keeps_file, write)`. It awaits
    the `db()` write under `asyncio.shield`.
  - **On a cancel** (Stop, the step timeout, a shutdown abort), it waits for the write's real
    outcome. It deletes the new file only if the write failed, or if the row does not use the
    file (`_record` returned `written=False`). Then it re-raises the cancel.
  - **An outcome it cannot learn** (cancelled again while waiting) keeps the file: an orphan
    file is harmless, a row without its file is not.
  - **The repair branch.** `_repair` now updates the document and points the resume at it in
    one transaction, so there is one outcome to wait for.
- **Why.** asgiref gives up on a `db()` thread at once on cancel, and the thread still commits.
  The old `except BaseException: delete(path)` then left a committed document whose file was
  gone: 404 "The file is missing from storage." on the PDF link.
- **Evidence.** `tests/resumes/test_render.py` holds the write in its thread and cancels the
  task meanwhile:
  - `::test_a_cancel_during_the_record_keeps_the_file_of_the_row_that_commits`: the row
    committed, the resume points at it, and the file exists;
  - `::test_a_cancel_during_a_record_that_fails_leaves_no_file`;
  - `::test_a_cancel_during_a_repair_keeps_the_repaired_file`.

  All three also assert that the task did not finish before the write did. With the shield
  removed, all three fail.

## 9. F9: a lane that loses the database is started again; the containers restart

- **What.**
  - **The worker.** In `manage.py worker`, each lane runs under `_run_lane`. A lane whose
    procrastinate Worker ends with a lost connection is started again after 1 s, doubling to
    30 s, while the worker is not stopping. "Lost" means a `ConnectorException` or a psycopg
    `OperationalError` (`PoolTimeout` included) anywhere in the cause chain. The backoff starts
    over after a lane has run 60 s. Each restart logs `lane <x> lost the database (…); starting
    it again in Ns`.
  - **Any other ending** still stops the worker with exit code 1, as before.
  - **Compose.** `db`, `api`, `worker` and `web` get `restart: unless-stopped`. Design §13.3
    says so.
- **Why.** procrastinate's heartbeat and fetch have no error handling. A Postgres restart longer
  than about 15–30 s ended a lane, `serve()` then stopped the others and exited 1, and nothing
  restarted it (`pnpm dev:worker`'s watchfiles restarts only on a file change). Every queued
  run, the reaper, approval expiry and follow-ups stopped until a manual restart. The legacy
  worker survived outages.
- **Evidence.**
  - `tests/runs/test_worker_shutdown.py::test_a_lane_that_loses_the_database_is_started_again`:
    the system lane's Worker raises `ConnectorException` (from a `PoolTimeout`) twice. It is
    started three times, the others once, and the logs say "again in 0.1s" then "0.2s".
    `serve()` returns 0 at the stop.
  - `::test_a_lane_that_fails_for_another_reason_still_stops_the_worker`: a `RuntimeError`
    still gives exit 1 at once.
  - `::test_only_connection_failures_count_as_a_lost_database`.
  - `tests/test_repo.py::test_long_running_services_come_back_on_their_own` checks the restart
    policies.
  - **A real outage** (a scratch probe, not committed, on `test_foray_app_h2o`):
    - `ALTER DATABASE … ALLOW_CONNECTIONS false` and `pg_terminate_backend` on every session
      for 40 s, from 3 s to 43 s.
    - At 63.6 s, 20 s after the database came back, all three lanes logged "lost the database
      (ConnectorException …)" and were started again.
    - `debug.sleep` jobs deferred after the outage reached `succeeded` on the same process.
    - `serve()` returned 0 only when stopped, at 134.5 s.
    - Under the same probe before the fix, the reviewer saw `serve()` exit 1 at 65 s.

## 10. F10: `tailor.started` and `render.failed` are written only while the run is active

- **What.** `apps/resumes/pipeline.py`:
  - `_tailoring` takes the run's lock (`_lock_if_active`) before it merges the stats and emits
    `tailor.started`. It returns False for a stopped run, and `tailor()` then returns before
    the paid draft call.
  - `render.failed` goes through `_render_failed`, which does the same.
- **Why.** A Stop landing while the tailor read its subject left `tailor.started` after
  `run.cancelled` (R4), and could start the model call.
- **Evidence.** `tests/resumes/test_pipeline.py`:
  - `::test_a_stop_before_the_draft_ends_the_tailor_without_a_model_call`: the Stop commits
    inside `_tailor_subject`. The events are exactly `['run.cancelled']`, the stubbed draft
    was never reached, and there is no ledger row.
  - `::test_a_stop_during_the_render_leaves_the_cancel_last`: the Stop happens during the
    render, which then raises. No `render.failed` follows the cancel.

  Each fails without its half of the fix.

## 11. F11 and open item 2: the follow-up names its approval, and re-checks it when posting

- **What.** `apps/chat/follow_up.py`:
  - **The id.** `ApprovalView` gains `id`. Every `approval.requested` update gains one line
    before the rules: `The pending approval is "<id>" (<kind>: <title>). When the user answers
    it in chat, pass that id to answer_approval; never guess one.` Without an id the text is
    byte for byte the TypeScript golden, so the 21 goldens still pass unchanged.
  - **The re-check.** `Post` carries `approval_id` for `approval.requested`. `post_follow_up`
    locks that approval (`select_for_update`, after the conversation) and returns `decided`
    when it is no longer pending. `run_follow_up` reports that as the `decided` skip.
  - **Lock order.** `decide` locks the run, then the approval. The follow-up locks the
    conversation, then the approval. No transaction locks an approval and then a conversation,
    so there is no deadlock.
- **Why.**
  - F11: answering from the card does not touch the conversation, so a reply generated
    meanwhile still asked "shall I go ahead?" after the review had started.
  - Item 2: the real model guessed approval ids (step-10.md §10).
- **Evidence.**
  - `tests/chat/test_follow_up_text.py::test_a_gate_update_names_its_approval`: three cases,
    and the text without the id equals the golden form.
  - `tests/chat/test_follow_up_db.py::test_a_gate_follow_up_names_the_proposed_roles` now also
    asserts the id line.
  - `::test_a_gate_answered_from_its_card_while_the_reply_is_written_is_not_posted`: `decide()`
    runs from inside the model's stream. The outcome is `skipped (decided)`, and no message is
    written.

## 12. F12: JSearch counts every attempt

- **What.**
  - `HttpClient.fetch_text` takes `on_attempt(n)`, awaited before each attempt, retries
    included.
  - `search_jsearch` counts each attempt with it (`count_request`, `requests_used`).
  - Before every attempt it checks the month's budget, using the count the last upsert
    returned. This covers each retry and each later page's first try. An attempt that would
    pass the budget raises `JSearchQuotaError` and is not sent. After a first page, that
    returns what the earlier pages found, as an `HttpError` does.
  - The first attempt of page 1 always passes this check, because the check before the loop
    has already refused a spent month.
- **Why.**
  - A retried page reached RapidAPI twice but was counted once, so the budget guard
    undercounted.
  - Round 1 checked the budget only for retries (`attempt > 0`). The page count is capped
    before the loop at `budget - used`, but a retry spends one of those pages. So a later
    page's first try could still go out over budget.
    - Verification round 1 had a budget of 5 with 2 already used, and page 1 was retried. It
      saw 4 requests sent and `api_usage` at 6.
    - Round 2 drops the `attempt > 0` condition.
- **Evidence.**
  - `tests/jobs/test_jsearch.py::test_every_attempt_counts_against_the_monthly_budget`: budget
    5, each page answering 503 then 200. 5 requests were sent and counted, page 3's retry was
    not sent, and 2 pages of jobs came back.
  - `::test_a_later_page_is_not_sent_once_a_retry_spent_its_share` (round 2) uses the
    verifier's case:
    - budget 5 with 2 already used and `max_requests=3`;
    - page 1 answers 503 then 200, and later pages answer 200.
    - Page 1 is sent twice and page 2 once. Page 3 is not sent. `api_usage` ends at 5, and 2
      pages of jobs come back.
    - With round 1's `attempt > 0` guard put back, it fails (`assert 4 == 3`).
  - `::test_a_failed_request_still_counts` now expects 2, its two real requests (it asserted 1
    while the mock was hit twice).
  - `::test_a_failure_after_a_page_keeps_what_it_has` expects 3.
  - `tests/jobs/test_http.py::test_on_attempt_runs_before_every_attempt`.

## 13. F13: a paid call always leaves its ledger row

- **What.** `apps/llm/ledger.py` `insert_row` writes the row as before. On an `IntegrityError`
  (a deferred foreign key failing at commit in autocommit), it checks which of the chat, run
  and user still exist. It then writes the row again without the missing ones and logs a
  warning. A user that is gone makes it a system row, which the budget guard still sums. If
  nothing is missing, the error is re-raised (and logged by `_record`, as before).
- **Why.** Deleting a chat while a search started from it was still paying for calls (or
  deleting a user mid-call) lost every later row of that step. The budget guard never saw that
  spend.
- **Evidence.** `tests/llm/test_ledger.py`:
  - `::test_a_call_whose_chat_is_deleted_meanwhile_keeps_its_row`: the chat is deleted inside
    the provider call. The row is kept with the user and the run, `conversation_id` NULL, the
    cost $0.000452, and the month-to-date spend equal to it.
  - `::test_a_call_whose_user_is_deleted_meanwhile_is_kept_as_a_system_row`.

  Both fail without the fix (`IntegrityError … llm_calls_run_id_…_fk_runs_id`, 0 rows).

## 14. F15: "Try again" on a run-update notice runs a read-only follow-up

- **What.** In `apps/chat/views.py`, `_write_user_message` returns the notice's metadata when
  the re-sent latest user-role row is a `run_update` notice. `prepare_turn` then makes the turn
  a follow-up:
  - `follow_up=True`: `FOLLOW_UP_TOOLS` and the follow-up's step cap;
  - `run_id` from the notice: ledger attribution, now also passed by `stream_turn`'s
    `llm_context`;
  - `reply_metadata = {kind: "follow_up", runId, eventId}`: `_save_reply` stores it on the
    reply.

  A re-sent message of the user's own is unchanged.
- **Why.** After a failed turn and a follow-up, the client's `regenerate()` re-sends the
  notice, which ran as an ordinary turn with all 16 tools (answer_approval included) on text
  the user never wrote. step-6.md §5 (the notice counts as the latest user message) still
  stands. This entry decides which tools such a turn gets.
- **Evidence.** `tests/chat/test_chat_view.py`:
  - `::test_try_again_on_a_run_update_notice_runs_a_read_only_follow_up`:
    - the tools offered are exactly `FOLLOW_UP_TOOLS`;
    - the old follow-up reply is replaced by one with `metadata == {kind: follow_up, runId,
      eventId}`;
    - the notice is untouched;
    - the ledger rows carry the run and the chat.
  - `::test_try_again_on_the_users_own_message_keeps_every_tool`: `TOOL_NAMES` on both turns,
    and no metadata.

## 15. F16: GPT-5.6 cache writes are priced at 1.25x input

- **What.** `PRICING`: `openai:gpt-5.6-luna` `cache_write=0.25` and `openai:gpt-5.6-terra`
  `cache_write=2.5`.
- **Not changed.**
  - genai-prices 0.1.9 lists lower cache-read rates for `gemini-3.1-pro-preview` (0.2 against
    our 0.5) and `gemini-3.8-flash` (0.075 against 0.19). Ours over-report, which is the safe
    side for the budget. No authoritative source was checked, and neither model is
    configured.
  - No boot check for an unpriced configured ref was added. `pricing` already logs one warning
    per unpriced ref, and the settings page shows `rate: null`.
- **Evidence.** `tests/llm/test_pricing.py::test_gpt_5_6_cache_writes_bill_at_one_and_a_quarter_times_input`
  (luna and terra). The review's example request costs $0.001330, the list price; it was
  $0.001180. `::test_cache_writes_bill_at_the_input_rate_where_not_priced_separately` now uses
  DeepSeek, which still has no separate write rate.

## 16. F17: the dropzone refuses a file over 10 MB before sending it

- **What.**
  - `src/lib/uploads.ts` holds `RESUME_MAX_BYTES` (10 MiB) and `resumeTooLarge(size)`. It
    returns Django's sentence ("That file is 20.0 MB; the limit is 10 MB.", using `toFixed(1)`,
    as `sniff.too_large_message` rounds) or null.
  - `ResumeDropzone.start` checks it after the extension check, before any request.
  - `next.config.ts`'s comment explains why.
- **Not changed.**
  - `proxyClientMaxBodySize` stays 16 MB. Raising it only moves the hang.
  - No upload timeout was added. With the client check, a browser never sends a body Next
    would truncate.
- **Evidence.**
  - `src/test/unit/uploads.test.ts`: exactly 10 MB is allowed; 10 MB + 9 bytes, 12.3 MB,
    10.25 MB and 20 MB give Django's own strings.
  - In the pane, on `/profile`: a 20 MB `scan.pdf` handed to the dropzone's file input showed
    "That file is 20.0 MB; the limit is 10 MB." within 300 ms. No `/api/uploads` request was
    made, and the zone never showed "Reading scan.pdf…".

## 17. Open item 1: the database is the one clock (migrations `00NN_h2_database_clock`)

- **What.**
  - **Model defaults.** Every model timestamp default is `db_default=Now()` (Postgres
    `STATEMENT_TIMESTAMP()`):
    - `Timestamped.created_at/updated_at`, so `users`, `conversations`, `messages`,
      `companies`, `job_matches`, `resumes` and `runs`;
    - `approvals.created_at`, `events.created_at` and `llm_calls.created_at`;
    - `documents.created_at`, `job_matches.stage_changed_at` and `api_usage.updated_at`.
  - **The migrations.** One per app: `accounts.0002`, `chat.0003`, `jobs.0002`, `llm.0002`,
    `matching.0003`, `resumes.0002`, `runs.0002`. Each only runs `ALTER COLUMN … SET DEFAULT
    (STATEMENT_TIMESTAMP())`.
  - **Writes.** `Timestamped.save()` writes `updated_at = Now()`. Django 6.1 reads it back
    with `RETURNING`, as it does a `db_default` on insert, so instances hold real values.
  - **Every Python-clock write of these columns is now `Now()`:**
    - `.update(updated_at=…)` in profile, chat tools and views, matching queries, the tailor,
      render and the registry;
    - `turn_started_at`;
    - the follow-up's `updated_at`;
    - `decided_at`;
    - `expires_at` (`Now() + 7 days`);
    - the import record's `finished_at`.
  - **Comparisons.** Every comparison with "now" is in SQL (`Now()` / `statement_timestamp()`)
    or against the new `apps.core.clock.database_now()`:
    - approval expiry: `decide`, `expire_lapsed`, `list_pending`, live and Home;
    - the follow-up's turn marker and staleness;
    - `acceptance_checks`' marker line;
    - the pages' month start (`clock.month_start()`), which costs one query, within every
      page's budget.
  - The `last_fetched_at` and `prefilter` dates were already compared with the clock that
    writes them, and are unchanged.
- **Why.** Step-8E.md §7.7: model defaults used Python's clock, raw SQL the database's, and the
  reaper's 60 s rule, `acceptance_checks`' 2-minute line, the budget's month and the follow-up's
  marker compared the two. The Docker VM's clock falls behind the host's after a sleep (8 s was
  seen).
- **Safe for the cutover import.**
  - Only defaults change. The importer writes every timestamp explicitly, and an explicit value
    is kept.
  - `tests/legacy` (the step 3F rehearsal path: import, `--replace`, `verify_import`,
    `reset_app_db`, `legacy_rehearsal`, backup) passes in the full suite.
  - `pnpm db:migrate` applied the seven migrations to `foray_app` in under a second.
- **Evidence.**
  - **`tests/clock.py` (verification round 3).** `python_clock(clock)` moves this process's
    clock for the skew tests. Patching `django.utils.timezone.now` alone never reaches a field
    declared `default=timezone.now`: Django keeps the function object on the field when the
    model class is built. So `python_clock` also swaps the `default` (and the cached
    `_get_default`) of every model field whose default is `timezone.now`, and restores both on
    exit. `skewed(delta)` is the real clock plus `delta`.
  - **New tests:**
    - `tests/core/test_timestamps.py::test_every_row_timestamp_defaults_to_the_databases_clock`
      (structural, round 3):
      - all 20 `created_at`/`updated_at`/`stage_changed_at` columns of the app models have no
        Python default and `db_default=Now()`;
      - the one `default=timezone.now` left is `legacy_imports.started_at`. The importer always
        sets it, and it is only displayed.
    - `tests/core/test_timestamps.py::test_stored_timestamps_ignore_this_processs_clock`, with
      Python's clock 90 minutes behind and 40 days ahead: users, runs, conversations, events
      and ledger rows all read the database's present;
    - `tests/runs/test_reaper.py::test_the_orphan_rule_reads_the_databases_clock`, at ±120 s:
      - a fresh unheld run is spared;
      - a run whose stored `updated_at` is moved back 70 s from whatever was written is
        failed. Round 3 changed this from `age_run`, which rewrote `updated_at` from SQL's
        `now()` and so hid a Python-clock default at +120 s.
    - `tests/llm/test_budget.py::test_the_month_is_the_databases_whatever_this_processs_clock_says`:
      a row written while Python believes it is last month counts in this month.

    **Correction (round 3).** Up to round 2 this said "the first two fail with the old
    `Timestamped` and `LlmCall`". That was false. Those tests patched only `timezone.now`, so
    the reaper and budget tests passed on the pre-H2 code. The timestamps test caught the old
    `save()`, whose `timezone.now()` call the patch did reach. With only the defaults put back,
    it failed only on sub-millisecond ordering, because this machine's database clock runs
    about 1.5 ms behind Python's. Where the VM's clock is ahead, it would have passed. The skew
    tests now use `python_clock`. With each pre-H2 default put back, they fail by the whole
    skew (§20, round 3).
  - **Tests that mixed the clocks themselves** now set compared timestamps with `Now()` or
    compare with `database_now()`:
    - in `tests/chat/test_follow_up_db.py`, `test_chat_view.py`, `runs/test_approvals.py`,
      `test_api.py`, `test_defer_in_tx.py`, `test_tasks.py` and `test_commands.py`;
    - the `make_approval` factory defaults;
    - the timestamp tests.
  - **Whole suite under simulated skew,** with step 8E's scratch plugin (`-p foray_skew`, which
    shifts `time.time`, `timezone.now` and procrastinate's `utcnow`): +8 s 1749 passed; −8 s
    1749 passed (rerun after the import-record fix); +120 s 1749 passed; −120 s all passed
    (1750: that run also collected the scratch outage probe of §9, which passed).
  - **The one failure on the way.** At −8 s, `test_acceptance_checks_skip_imported_runs` failed
    because the import record's `finished_at` was still Python's. That is why it is `Now()`
    now.

## 18. Open items 3 to 8

- **3. The continuation's phase moves with the decision.**
  - `approvals._resume(run_id, phase)` is one conditional `UPDATE … status='running', stats =
    stats || {"phase": …}`.
  - A yes writes `CONTINUATION_PHASE[next_step]`: `evaluating` for `search.evaluate`,
    `shortlisting` for `search.shortlist`. A no or a lapse writes no phase: its continuation
    completes the run.
  - Evidence: `tests/runs/test_approvals.py::test_a_yes_moves_the_run_to_its_continuations_phase_with_the_decision`
    covers both gates and a no. The R6 tests pass.
- **4. The chat fake reads "add <name>" before "matches/jobs".** Evidence:
  `tests/chat/test_chat_view.py::test_the_chat_fake_adds_a_pasted_board_link`. "add
  https://jobs.ashbyhq.com/linear" calls `add_company` with the link, and "Show me my matches"
  still lists.
- **5. Each board refresh shows twice: already fixed on the branch** (step-10.md §3).
  `company.refresh.done` is emitted at `debug`, `tests/jobs/test_pipeline.py` pins it, and
  this step's full suite passes. No change.
- **6. The Role column at 1,024 px: already fixed** (step-10.md §6, Location from `xl`). In the
  pane at 1024×800 this step measured Role at 252 px, and the Location header at 0 px (hidden).
  No change.
- **7. The two TypeScript reference turns moved with the goldens.**
  - `git mv docs/migration/prototypes/ts-turn{1,2}.{sse,persisted.json}` to
    `backend/tests/goldens/uistream/prototypes/` (the renames are staged).
  - `test_uistream.py`'s `PROTOTYPES` is `GOLDENS / "prototypes"`, and the goldens README
    lists the files.
  - `grep -rn docs/migration backend src` finds only comments (`test_models.py`,
    `test_jsonio.py`). No code reads the folder. The other prototypes stay in
    `docs/migration/prototypes/` as references and go with step 13.
- **8a. A failed tailor offers a retry without a reload.**
  - `tailor-panel.tsx` reads the run with `useRun`. A `failed` or `cancelled` run no longer
    holds the panel on its card: the card stays, to say why, and the panel shows its button
    again. The button reads "Try again" when no tailored resume exists yet, and "Tailor again"
    when one does.
  - Evidence, in the pane: `LLM_FAKE_FAIL=tailor` on the worker, then "Tailor my resume" on a
    throwaway match. The card read "Tailoring failed: Fake failure", with "Try again", within
    one poll. "Try again" went Stop → Try again with a window marker kept (no reload), and
    produced exactly one toast. No unit test: the frontend has no component test harness.
- **8b. A failed chat turn's "Try again" after a reload: recorded, not done.**
  - The error lives only in `useChat`'s state. A failed turn stores the user's message, and
    any partial reply, but no error.
  - Persisting it needs:
    - a stored failure on the turn;
    - the page to serve it;
    - the client to tell a failed turn from one still streaming in another tab (the server's
      200 s `turn_started_at` marker), or a "Try again" there would start a second concurrent
      turn.
  - That is three layers and a new race, which is not small. After a reload the user can send
    the message again, and the next turn works.

## 19. F14: partly done; the rest is recorded

- **Done.**
  - A notice can never run with write tools any more (F15).
  - Design §7.2's claim is corrected. A posting's full text is wrapped. Short posting fields
    (titles, companies, locations, headlines, evaluation quotes, approval items) reach the
    model unwrapped in `list_jobs`, `get_job`, `list_pending_approvals` and the stored notice.
  - DESIGN.md's Known risks gains the row.
- **Not done.**
  - **Wrapping those fields.** It would change the tool outputs the chat UI renders as cards,
    and the follow-up notice whose text is pinned byte for byte by the TypeScript goldens.
  - **A structural confirmation for write tools** (approve, preferences, archive, add
    company). The suggested rule, "the latest stored message is a genuine user message posted
    after the approval", passes in the attack's own scenario: the user's next message always
    comes after the approval. A real design (route decisions through the card, or an explicit
    confirmation turn) is a product decision for a later phase.
- **Why low (the verifier's grounds).** It is not a regression from main. The notice says it
    is not from the user, follow-ups are read-only, and the damage is bounded by the budget and
    by reversible stage moves.

## 20. Checks run in this step

- **pytest**, `FORAY_REGISTER_STRICT=1`, `FORAY_TEST_DB_NAME=test_foray_app_h2 --create-db`:
  - before the step: 1710 passed;
  - after: **1749 passed** in 82 s, with the one third-party `google.genai`
    `DeprecationWarning`. The 39 new tests are the ones named above, and no register row is
    pending.
- **Skew.** +8, −8, +120 and −120 s all passed (§17).
- **Static.**
  - `ruff check` and `ruff format --check` are clean, and pyright reports 0 errors.
  - `next typegen`, then `tsc --noEmit`, and `eslint` exit 0.
  - Vitest: 3 files, 22 tests.
  - `pnpm build` exits 0 with 13 routes.
  - `pnpm api:types`, then `git diff --exit-code backend/openapi.json src/lib/api/schema.d.ts`:
    no drift. No API schema changed.
  - `makemigrations --check` reports no changes.
- **Mutations** (a scratch script, `scratchpad/h2_mutate.py`; each fix undone in its file, its
  tests run, the file restored byte for byte). 23 of 23 fail without their fix:
  - F1, F2 compose, F2 next dev, F3, F5, F6, F7 refusal, F7 reminder;
  - F8, F9, F10 started, F10 render, F11, F12, F13, F15, F16, F17;
  - items 1, 2, 3, 4 and 7.

  Item 1's mutation here put `core/models.py` and `llm/models.py` back to HEAD. That restored
  the Python-clock defaults and `save()`'s `timezone.now()` together. The failure it recorded
  came from `save()`, which the old patch did reach. Round 2's verifier showed that the
  defaults alone went uncaught (§17's correction). Round 3 below mutates each default
  separately.
- **Pane smoke test.**
  - **Setup.**
    - `pnpm dev:all` ran in its own process group with `LLM_FAKE=1 LLM_FAKE_FAIL=tailor
      JSEARCH_ENABLED=false`, through a line timestamper. `.env` was not touched.
    - `create_test_user --email h2-smoke@example.test --with-resume --with-match`, then
      `pnpm dev:session`, with the cookie set with `document.cookie`.
    - `/api/me` answered 401 before the cookie and 200 after.
  - **Pages.**
    - Home: "1 role in your queue.", the board counts and "This month / Model spend $0".
    - Matches list at 1024 px (§18.6).
    - Match page tailor failure and retry (§18.8a).
    - Profile dropzone (§16).
    - Settings: "Counted from October 1, in UTC.", spend and "Last heartbeat just now."
  - **Chat.** "What's on my resume?" was typed into the focused composer and sent with Enter.
    The address became `/chat/<id>` with the window marker kept. The reply showed the "Read
    your resume" line and "Here is the top of your resume: Name: Avery Quinn.".
  - **Errors.**
    - Console: only the deliberate 401 before the cookie.
    - Logs: only the two forced `Fake failure` tailor tracebacks and the signed-out `/api/me`
      401s.
- **Money.** $0. This step wrote 4 `fake` ledger rows (2 tailor, 2 chat) at $0. JSearch was
  off, and `api_usage` stayed at 17.
- **Afterwards.**
  - SIGINT to the process group: the worker logged `stopping` then `stopped`, uvicorn
    "Finished server process", and `pnpm dev` exited.
  - :3000, :8000 and :8001 are free.
  - `create_test_user --purge` printed "Deleted 1 test user.".
  - `foray_app` has 0 users, sessions, runs, conversations, documents and worker rows, and no
    todo/doing jobs.
  - The pane's tab was closed. The `seed` tab, from another session, was left alone.
- **Legacy `foray`.** The write counters were 41158/1014040/5351 before and after. `git
  --no-optional-locks -C <main checkout> status --porcelain` is `?? reports/` and
  `?? research_notes/`.
- **Test databases for the orchestrator to drop:**
  - `test_foray_app_h2`;
  - `test_foray_app_h2s` (the skew runs);
  - `test_foray_app_h2o` (the outage probe, with `ALLOW_CONNECTIONS` set back to true).

### Verification round 2

Round 1's independent verification failed on two findings. F1 was medium: dot segments could
resolve to a leading `//`. F12 was low: a later page's first try skipped the budget check.
§1 and §12 above record the fixes. Checks run after them:

- **pytest**, `FORAY_REGISTER_STRICT=1`, `FORAY_TEST_DB_NAME=test_foray_app_h2r --create-db`:
  **1750 passed**. That is 1749 plus the new F12 test, with the same one `google.genai`
  `DeprecationWarning`.
- **Static.** `ruff check`, `ruff format --check` and pyright (on `jsearch.py` and its tests)
  are clean. `tsc --noEmit` and `eslint` exit 0.
- **Vitest.** 3 files and 23 tests pass, 5 of them in `safe-next.test.ts`.
- **Build.** `pnpm build` exits 0 with 13 routes.
- **API.** No API schema changed.
- **Mutations.** Each fix was undone in its file, its tests run, and the file restored byte for
  byte (`cmp`):
  - **F1.** Returning the resolved path unchecked fails 2 of the 5 Vitest cases.
  - **F12.** Putting `attempt > 0` back fails the new test with `assert 4 == 3`.
- **Live.** §1 gives the curl and pane results.
  - Only `pnpm dev` and `pnpm dev:api` ran, with `LLM_FAKE=1 JSEARCH_ENABLED=false`, and they
    were stopped with SIGINT.
  - The pane's tab was closed. The `seed` tab, from another session, was left alone.
  - The throwaway user `h2r-next@example.test` was removed with `create_test_user --purge`
    ("Deleted 1 test user.").
  - `foray_app` has 0 users, sessions and runs.
  - :3000, :8000 and :8001 are free.
- **Money.** $0. No model was called and JSearch was off.
- **Cleanup.** `test_foray_app_h2r` was dropped.
- **Legacy `foray`.** The write counters are still 41158/1014040/5351.

### Verification round 3

Round 2's independent verification failed on one low finding about item 1. Its skew tests
patched only `timezone.now`, which never reaches a `default=timezone.now` field. So the reaper
and budget tests passed on the pre-H2 code, and §17's claim that they failed was false. The
product code was right: the verifier's scratch plugin, which patches before the models import,
caught the old defaults. No product code changed in this round.

- **The fix.**
  - `tests/clock.py` adds `python_clock` and `skewed`, which move field defaults too (§17).
  - The three skew tests use them.
  - The orphan-rule test moves the quiet run's stored `updated_at` back 70 s instead of
    rewriting it with SQL's `now()`.
  - The new structural test pins every row timestamp to `db_default=Now()`.
  - §17's evidence is corrected.
- **Mutations** (scratch `scratchpad/h2f/mutate_clock.py`). Each pre-H2 Python-clock default
  was put back on its own, the four clock tests were run, and every file was restored and
  checked by SHA-256:

  | Put back | Fails |
  |---|---|
  | `Timestamped` defaults only | structural; timestamps −90 min and +40 d (`users.created_at` 23:47:55 against the database's 01:17:55, then 2026-11-19); orphan rule −120 s (`assert 2 == 1`: the fresh run was failed too) and +120 s (`assert 0 == 1`: the quiet run was spared) |
  | `Timestamped` defaults and `save()` | the same five |
  | `save()`'s `timezone.now()` only | timestamps −90 min and +40 d (`users.updated_at`) |
  | `LlmCall.created_at` | structural; timestamps ×2; budget month (`2026-09-30 21:00 >= 2026-10-01` is false) |
  | `Event.created_at` | structural; timestamps ×2 |
  | `Approval.created_at`, `JobMatch.stage_changed_at`, `Document.created_at`, `ApiUsage.updated_at` (each alone) | structural |

  Every failure is by the whole skew, so none depends on the sub-millisecond gap between the
  two clocks. With the defaults put back, the budget test passes: it writes no `Timestamped`
  row. The orphan rule passes with `save()` alone put back: it saves no run.
- **Restore.** `python_clock` restores `timezone.now`, each field's `default` and its
  `_get_default` cache, whether or not the cache was filled before. A scratch check confirmed
  this on `legacy_imports.started_at`.
- **pytest**, `FORAY_REGISTER_STRICT=1`, `FORAY_TEST_DB_NAME=test_foray_app_h2f --create-db`:
  **1751 passed**. That is 1750 plus the structural test, with the same one `google.genai`
  `DeprecationWarning`.
- **Static.** `ruff check`, `ruff format --check` and `pyright` (the whole backend) are clean.
  Only tests and this file changed, so the frontend, the build and the API schema are
  unaffected.
- **Money.** $0. No server or worker was started, and no model was called.
- **Cleanup.**
  - `test_foray_app_h2f` was dropped.
  - :3000, :8000 and :8001 are free.
- **Legacy `foray`.** The write counters are still 41158/1014040/5351. The main checkout's
  status is unchanged (`?? reports/`, `?? research_notes/`).
