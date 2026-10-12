# Step 13: deviations and clarifications

Later steps read this before the design. Each entry: what, why, evidence.

Step 13 is the final acceptance on the user's real data (design §15.3, real-user rules in its
setup). Round 1 ran every item; this file records what its failures turned into. Three were code
defects, fixed here with a test that fails without the fix (§1–§3). The rest were the host
sleeping mid-run, or state round 1 changed on the real user (§4–§6).

Files changed:

- **backend:** `apps/runs/reaper.py`, `apps/runs/management/commands/worker.py`,
  `apps/runs/lifecycle.py`, `apps/matching/api.py`, `apps/matching/queries.py`,
  `openapi.json`;
- **backend tests:** `tests/runs/test_reaper.py`, `tests/matching/test_search_pipeline.py`,
  `tests/matching/test_api.py`;
- **frontend:** `src/components/matches/board.tsx`, `src/components/matches/match-api.ts`,
  `src/lib/api/schema.d.ts`.

Checks after the three fixes: `FORAY_REGISTER_STRICT=1` pytest 1,754 passed; Vitest 23 passed;
`pnpm lint` (ESLint, `ruff check`, `ruff format --check`) and `pnpm typecheck` (tsc, pyright)
clean; `pnpm api:types` regenerated both files; `pnpm build` succeeded.

## 1. Fix: the stalled-job sweep no longer fails the live worker's own jobs (`reaper.py`)

- **What.**
  - A worker middleware, `reaper.track_running`, records each job while this process runs
    it (job id → the lane's worker id). `manage.py worker` passes it to every lane as
    `worker_middleware`. It is not a `task_middleware`, so Django's per-task connection cleanup
    stays in place.
  - `fail_stalled_jobs()` now skips any stalled job that this process is running.
  - If any of this process's own workers has a heartbeat older than `STALL_SECONDS`, it skips the
    whole sweep and logs "stalled-job sweep skipped: …". In that case the machine was paused,
    so other processes' heartbeats are stale for the same reason. The heartbeats catch up within
    10 s, and the next reap, a minute later, judges again.
  - The startup sweep runs before any lane, when the set is empty, so it behaves as before.
- **Why.**
  - The laptop slept with its lid closed (round 1). On waking, every heartbeat was minutes old.
  - The periodic deferrer queued the missed reap ticks at once. That happened before any lane's
    heartbeat task had run, since its `asyncio.sleep(10)` resumes where it stopped.
  - The first reaps then failed:
    - the search and tailor jobs this same worker was running (jobs 143 and 454);
    - a `chat.follow_up` (335);
    - their sibling reaps, and themselves (312, 318, 321, 494, 507, 511, 513).
  - `reap_runs` then failed the runs nothing held: "Interrupted: the worker stopped…".
  - A heartbeat is evidence only about another process. A process that is running the sweep is
    alive by definition.
  - Pruned rows: a lane that starts again (after losing the database) prunes stale worker rows,
    its siblings' included. Their jobs are then left with `worker_id` NULL, which the
    jobs table reads as orphaned. Excluding the jobs this process is running covers that case
    too.
- **Evidence.** `tests/runs/test_reaper.py::test_a_sleep_never_fails_what_this_worker_is_running`
  runs the real `serve()` (lanes and middleware) with heartbeats written only at registration:
  1. Every heartbeat of this process is set 10 minutes old. A reap skips the sweep: the step's job
     stays `doing`, its run stays `running`, and a dead worker's job waits.
  2. The heartbeats are fresh again, but the step's worker row is deleted, as a prune does. A reap
     fails the dead worker's job and spares the step.
  - Undoing the fix fails the test. With the original code, the reap failed the step, the dead
    job and itself (log: "failed stalled job 3 (maintenance.reap…)"), which is the field symptom.
    Without the exclusion, part 2 fails. Without the pause check, part 1 fails.

## 2. Fix: a run that ended for any reason stops its pipeline (`lifecycle.is_cancelled`)

- **What.** `lifecycle.is_cancelled(run_id)` is now true once the run is no longer active
  (`queued`, `running`, `awaiting_approval`). Before, only `cancelled` counted. A missing run also
  counts as stopped. The name is kept: it is the pipelines' one stop check, between stages,
  batches and items (search, evaluate, resumes, companies).
- **Why.** The reaper failed search run 01a12da4 while its task was only frozen by the sleep. The
  task woke and kept scoring, because `fail()` is not a `cancel()` and nothing aborted it:
  - a ledger `score` row at 03:23:02Z (latency 477,905 ms), 45 minutes after the run's
    `run.failed`;
  - `fast_score = 78` written to one of its matches.

  CLAUDE.md says to "write nothing once a run has stopped". §1 makes this path rare, but any
  other `fail()` of a live run (say a step's own `fail_if_abandoned` race, or an operator) would
  spend the same way.
- **Evidence.**
  `tests/matching/test_search_pipeline.py::test_a_run_failed_during_scoring_stops_spending`:
  - setup: one posting per batch, and the run failed by the reaper's `fail()` right after the
    first score;
  - with the fix: one `score` call, one match scored, `run.failed` as the last event, no approval;
  - with the old check: four calls (`assert 4 == 1`).

## 3. Fix: the board's Undo puts back when the match entered its stage (PATCH `/api/matches/{id}`)

- **What.**
  - `MatchPatch` takes an optional `stageChangedAt`, an aware time. It is accepted only with
    `stage`; on its own, or without a zone, the answer is "That change could not be read.".
  - `set_match_stage(…, entered_at=)` stamps `LEAST(coalesce(entered_at, now()), now())` when
    the stage changes, and still keeps the timestamp when it does not.
  - The board records the match's `stageChangedAt` when a drag starts, if the server's row is
    still in the source stage. Its Undo sends that time back. After a move the page has not
    refreshed yet, the Undo stamps now, as before.
  - `BoardMatch` now picks `stageChangedAt`.
- **Why.** Round 1 dragged the real match c5e1fcf5 to Shortlisted and pressed Undo. The match
  went back to Found but read "since 18m ago" instead of Sep 25, because the Undo was a second
  move stamped `now()`. That is the opposite of item 1 ("the imported 'found … ago' dates, not
  today's"), and item 5 runs on the real user. The TypeScript app (main's `board.tsx`) behaved
  the same.
- **Evidence.**
  `tests/matching/test_api.py::test_an_undo_puts_back_when_the_match_entered_its_stage` covers:
  - move, then Undo with the original time, restores `2026-09-25 23:03:34.6283+00` exactly;
  - the same stage again keeps it;
  - a future time is clamped;
  - both unreadable bodies answer 400.

  On the old code the restored time is today's (the field symptom).

## 4. Not defects: host sleep blocked items 3, 4, 6 (in flight), 7, 11 (timing) and 12

The round-1 items that failed or were not run were blocked by the environment. They were not
blocked by a fault in what they test:
- the lid was closed on battery, so the Mac slept;
- the worker and the Docker VM paused;
- on waking, the reaper (before §1) failed the live runs.

They were run again with the machine awake: the session held a keep-awake, and the lid was open.
§7 has the results.

## 5. Real-user state round 1 left changed (not restored by this step: the database writes were refused)

The snapshot diff found two changes. Both were made by round 1, not by the user. This step's
guarded restores were refused by the session's permission classifier ("Modify Shared Resources").
They are left for the owner to run, and nothing else was written to the real user's rows:

```sql
-- (a) item 5's drag + Undo (before §3): put back the imported stage time of match c5e1fcf5.
--     The values are the legacy database's (`foray`, read-only), which the import copied.
UPDATE job_matches
   SET stage_changed_at = '2026-09-25 23:03:34.6283+00', updated_at = '2026-09-25 23:04:06.136+00'
 WHERE id = 'c5e1fcf5-f238-4985-b708-ebc9594fdb8f' AND stage = 'found'
   AND stage_changed_at = '2026-10-12 00:36:06.313412+00';
-- (b) item 10's save wrote today's defaults explicitly; the snapshot had '{}'.
UPDATE users SET settings = '{}'::jsonb
 WHERE id = 'AkYcEgCR5vnvABvn0fIC3sZC0ODUhvTm'
   AND settings = '{"defaultMode": "approve_each", "deepEvalTopN": 15, "scoreThreshold": 55}'::jsonb;
```

(b) is cosmetic: the effective values are equal, so the threshold Done check holds. (a) is
visible: the match page reads "since …" today. Each statement is guarded on the exact value
round 1 left, so a change the user has made since is not overwritten.

## 6. The JSearch counter lost October's pre-import usage (data, not a code defect going forward)

- **What happened.** The import (`--replace`) replaced `foray_app.api_usage` with the legacy
  database's rows. Legacy had only `jsearch 2026-09 = 25`, so the October requests that the
  pre-import development database had counted were lost:
  - at least 17 per step 10 §11;
  - about 42 by the provider, per the orchestrator.

  The guard now counts from 3 (the requests since the import) against its 180 budget.
- **Why there is no code fix.** The quota belongs to the API key, not to a database, and only the
  one-off import replaced the counter. The cutover is now marked, and the importer refuses to run
  again (`--replace` and `reset_app_db` refuse).
- **Seeding.** The seed write was refused like §5. The owner should run it before many more
  searches, with the dashboard's figure if it differs from 42:

  ```sql
  UPDATE api_usage SET requests = requests + 42, updated_at = now()
   WHERE provider = 'jsearch' AND period = '2026-10';
  ```

  Adding to the current value keeps the requests counted since the import (including this
  step's reruns). The RapidAPI dashboard has the exact figure; its billing month may not be the
  calendar month the guard uses.

## 7. Items run again (throwaway users, the fixes live)

The items were run in the browser pane (`foray-web`) on real models, with the worker running
the fixed code (pid 80323, then 82471 after item 11's kill). Two throwaway users were used:
- `test-ffmoxbpyxc@example.test` (`--with-resume --with-match`) for items 3, 4, 6, 7 and 11;
- `test-q4in0cmv3j@example.test` (`--with-resume`) for item 12.

Each had `deepEvalTopN` set to 3 to bound evaluate cost. Toasts were counted with a
`MutationObserver` on `[data-sonner-toast]`, and `foray:live-refresh` was dispatched every 1.5 s.
Times are UTC, from the database unless marked.

- **Item 3: pass.**
  - "Find entry-level software engineering roles in the Bay Area" in `/chat` started run
    01a12eb7 at 06:05:59.
  - The live RunCard advanced, without a reload: "Refreshing job boards" (5 s), "Checking the
    job aggregator…" (30 s), "Scoring 120 of 327 roles" (1 m 21 s).
  - At gate 1 (06:08:36), the divider "Search paused for your go-ahead", the agent's reply and
    the inline approval "Deep-evaluate 3 roles (about $0.09)" appeared in place.
  - No toast was on screen at either read. From 06:09 to the run's end the observer saw none.
- **Item 4: pass.**
  - Unticking Temporal changed the button to "Review 2 roles". Approving wrote `approvals.status
    = approved` and the event "Approved deep evaluation of 2 of 3 roles." (06:09:09). The card
    then read "Reading 0 of 2 postings in full" within 4 s.
  - The approval card's own "Approved. Foray is on it." was not caught: the card unmounts as soon
    as live state leaves `awaiting_approval`.
  - The gate-2 follow-up arrived at 06:09:28: the "Deep review done" divider and "Should I
    shortlist both?".
  - The reply "yes, shortlist them" showed the `answer_approval` line "Approved: shortlisting 2
    roles.". A 30 ms poll of `/api/live` saw `awaiting_approval/awaiting_shortlist` →
    `running/shortlisting` → `completed/done` 30 ms later. Then the "Search finished"
    follow-up listed the strongest matches.
  - Activity reads "327 scored, 2 shortlisted". As step 10 §8 explains, "N strong" shows only
    for a search that shortlisted nothing: the later New York search, skipped at gate 1, reads
    "203 scored, 25 strong".
- **Item 6: pass, this time caught mid-flight.**
  - "Get a deep review" showed the RunCard "Deep review / Reading the posting in full against
    your resume / 2s / Stop".
  - On completion the page refreshed in place: "Review again", score 12 "Weak fit · Skip", the
    seniority, location and pay chips, "What lines up" and "Gaps".
  - Exactly one toast: "Evaluated Staff Data Scientist… at airwallex: 12, probably not worth
    applying.".
  - `/activity`: "1 reviewed". Expanded, its events are in order, then "Took 11s" and "Cost
    $0.02".
- **Item 7: pass.**
  - "Tailor my resume" (Asana) showed the phases "Rewriting your resume for this role" →
    "Checking every claim against your resume" → "Rendering the PDF".
  - Exactly one toast at 06:12:46 (browser clock): "Your resume for Software Engineer, Data
    Loading Infrastructure at Asana is ready. It fits on one page.".
  - The pane's browser blocks the sandboxed preview frame (`net::ERR_BLOCKED_BY_CLIENT`; step 10
    §8). Headless Chrome with the throwaway's cookie (scratch `preview_check.py`) showed:
    - the frame answered 200 and rendered the resume ("Avery Quinn / Backend software
      engineer…");
    - there were no console errors and no CSP messages;
    - the frame sends `content-security-policy: default-src 'none'; …`.
  - The PDF: `/api/documents/<id>` answered `inline`, and `?download=1` answered `attachment`.
    Both were `application/pdf`, started `%PDF-` and were 49,241 bytes.
  - "Every claim checked against your resume." showed, and "Show what changed (2)" listed
    Summary and Skills, each with the before, the after and the reason.
- **Item 11: pass.**
  - The run list:
    - Activity lists runs with their targets.
    - An upload shows its file name: "Profile import / avery-quinn-asana.pdf". The upload went
      through Profile's file input; its toast read "Read 3 roles, 2 schools and 11 skills.".
    - Expanding the upload lists its event, "Took 7s" and "Cost <$0.01".
    - The debug switch raised the "Scored N of M" lines in the log from 0 to 16. It was switched
      back off.
    - "A company add shows its query" was confirmed in round 1 on the real user's `/activity`
      ("Company discovery / notarealcompany12345", "… / Linear").
  - Kill and restart:
    - A Mixpanel tailor started at 06:18:56. Its job 886 was `doing` on worker 53.
    - `kill -9` hit worker pid 80323 at 06:19:03.3 (host clock).
    - `touch backend/apps/runs/tasks.py` (06:19:11) brought the worker back as pid 82471 at
      06:19:12. Its startup sweep failed 0 jobs, because the dead worker's heartbeat was not yet
      30 s old.
    - The periodic reap at 06:20:00.540 failed job 886. At 06:20:00.545 it failed the run with
      "Interrupted: the worker stopped while this was running. Start it again to retry." That is
      57 s after the kill, with exactly one error toast.
- **Item 12: pass.**
  - Stop was pressed on "Scoring 40 of 327 roles" (06:21:17.766, browser clock). The run was
    `cancelled` at 06:21:17.781.
  - The Stop's answer toasted "Stopped. Everything found so far was kept.". The card turned to
    that sentence with "Run again".
  - `run.cancelled` (event 2097) is the run's last event, and none follows it.
  - The run's `llm_calls`: 69 rows. 16 in-flight calls ended as `cancelled (input estimated)`
    within 52 ms of the cancel, and none was created more than 5 s after it. No match was
    written after the cancel.
  - A first attempt (a Seattle search with 12 roles) finished scoring before the Stop landed, at
    gate 1, so it does not count.
- **Afterwards.**
  - `acceptance_checks --since 2026-10-12T00:21:00Z`: 8 PASS.
  - Spend by the ledger since 00:21Z: $0.502648 over 667 rows ($0.467 this round). That is
    under the $1.50 cap.
  - The JSearch counter went from 3 to 13: three searches with 2 to 3 requests each, and
    round 1's 3 before them.
  - The legacy `foray` write counters (public schema) stayed at 41158/1014040/5351.
  - `create_test_user --purge` reported "Deleted 2 test users.". No active runs and no todo or
    doing jobs remain.
  - The pane was signed back in as the real user (a `dev:session` cookie after a sign-out) and
    shows Home's imported board.
  - The real user's 284 matches still differ from the snapshot only in c5e1fcf5 (§5). The
    conversation "hello" is untouched.
