# Step 4: deviations and clarifications

Later steps read this before the design. Each entry: what, why, evidence.

## 1. The periodic schedule is installed by `manage.py worker`, not with `@app.periodic`

- **What.** `apps/runs/periodic.py` holds the §9.3 schedule (`SCHEDULE`: task name, cron, queueing
  lock). `manage.py worker` calls `periodic.install(app)` before it starts its Workers; nothing
  registers a periodic task at import. An entry whose task is not registered yet is skipped with a
  log line (`companies.refresh_scheduled` until step 5C lands); an entry whose owner registered it
  with `@app.periodic` is left alone. A periodic task receives the tick it was deferred for as a
  `timestamp` keyword, so **step 5C's `companies.refresh_scheduled` must accept
  `timestamp: int | None = None`**.
- **Why.** Every procrastinate Worker runs the periodic deferrer, including the in-process Workers
  of `tests/queue.py` (`run_deferred`). With the schedule registered at import, every test in
  every lane that drains the queue would also defer, and usually run, the reaper, the expiry
  sweep, the prune and (after 5C) a network refresh, making `deferred()` assertions and run states
  flaky. The design fixes the schedule, not the registration mechanism.
- **Evidence.** `tests/runs/test_tasks.py::test_no_periodic_task_holds_a_lock` (installs into a
  fresh registry, no `lock`, a `queueing_lock` on each, idempotent). Live: each worker start
  logged `startup: periodic maintenance.reap, approvals.expire, maintenance.prune` and
  `periodic companies.refresh_scheduled skipped: the task is not registered yet`.

## 2. Tasks deferred by name before their owner lands (contract for steps 5A and 6)

- **What.** `step.task_for(name)` returns the registered task, or, for a name in `step.TASKS`
  that is not registered in this process, a stand-in `Task` with that name and lane, so the job
  row is exactly what the real task would write. `approvals.next_step` continuations
  (`search.evaluate`, `search.shortlist`) and the follow-up (`chat.follow_up`) are deferred this
  way. The job kwargs are fixed here:
  - a continuation: `run_id` (str), `approval_id` (str); `lock="run:<run_id>"`,
    `queueing_lock="approval:<approval_id>"`, lane `background`;
  - the follow-up: `event_id` (int) only; no lock, `queueing_lock="follow-up:<event_id>"`, lane
    `interactive`.
  When the owner's task is registered (procrastinate's autodiscovery imports every app's
  `tasks.py` at startup), it is used instead; a worker that does not have it fails the job
  (`TaskNotFound`) and the reaper then fails the run.
- **Why.** `request_approval`, `decide`, `expire_lapsed` and `emit` must defer these tasks, and
  their R1/R6/R7 tests must see the jobs, but the tasks belong to steps 5A and 6.
- **Also.** `step.TASKS` is the §9.2/§9.3 task table (lane and timeout per name).
  `tests/runs/test_tasks.py::test_every_task_sits_on_its_lane` fails for any registered task that
  is not in it or not on its lane, and `::test_money_tasks_never_retry` (R11) requires `retry=0`
  everywhere except `resume.render` (`retry=1`), the maintenance tasks and the ping, and
  `chat.follow_up`, which must use `step.shutdown_only_retry()` (handoff to step 6). Timeouts the
  design left open: `maintenance.reap` and `approvals.expire` 5 min, `maintenance.prune` 30 min,
  `companies.refresh_scheduled` and `debug.sleep` 60 min.

## 3. `request_approval`, `decide`, `cancel`, `complete`: signatures and choices

- **`request_approval(run_id, kind, title, payload, next_step=None, *, stats=None, data=None,
  messages=None)`.** `next_step` defaults to `APPROVAL_NEXT_STEP[kind]`; `messages` is TS's
  `{requested?, auto?}` (step 5A's gates use both); the mode and user come from the run, which is
  locked first. The gate needs a `running` run: otherwise (cancelled meanwhile) the approval is
  inserted already `expired`, with no event and no continuation, in autopilot too (an
  `approval.auto` after `run.cancelled` would break R4). Event data is `{kind, **data}`.
- **Lock order run → approval** everywhere (`decide`, `cancel`, `_expire`), so a concurrent decide
  and Stop cannot deadlock; `expire_lapsed` locks both rows in one `FOR UPDATE OF a, r SKIP
  LOCKED`. `decide` reads the approval's run id first without a lock, then locks the run and the
  approval.
- **Expiry.** The sweep (and the inline lapse in `decide`) sets `status='expired'`,
  `decided_via='expiry'`, `decided_at`; a paused run resumes with its continuation and
  `approval.expired`. When the run has already ended, the approval lapses quietly (no event), so
  the run's terminal event stays its last. `cancel` expires pending approvals with
  `status='expired'` only, as `expireRunApprovals` did.
- **`cancel(user_id, run_id) -> bool`** as designed; `POST /api/runs/{id}/cancel` tells 404 from 409
  by an existence check after a `False`. Malformed ids are not found (`lifecycle.as_uuid`) in
  `cancel` and `decide`, never a database error (TS's `decideApproval(…, "approval-7")` case).
- **`complete(run_id, output, message, *, level="info", stats=None)`** merges
  `{"phase": "done", **stats}` into `stats` (TS's `finishActiveRun(…, {phase: "done"})`); a `None`
  output is stored as SQL NULL. `merge_stats`/`merge_input` take a mapping and/or keywords.
- **`start_run(..., task=...)`** accepts a task or a task name; `dedupe_key=None` never dedupes.
  On `runs_one_active_per_key` it returns the oldest active run with that key; if that run
  finished in between, it inserts again (at most three tries).

## 4. `LiveRun.summary` comes from `apps.matching.brief` (handoff to step 5A)

- **What.** For a search, `live.search_summary` imports `apps.matching.brief` at call time and
  returns `describe_brief(parse_brief(input.brief ?? input.hints))`, or `None` while that module
  or those two functions do not exist.
- **Why.** `toLiveRun` called `describeBrief(parseBrief(…))`, which belongs to step 5A.
- **Handoff.** Step 5A names them `parse_brief(value) -> SearchBrief` (lenient) and
  `describe_brief(brief) -> str | None`.

## 5. `GET /api/live?since=` reads `since` like JavaScript's `Number()`

- **What.** `since` is a string query parameter; `live.parse_since` floors a finite positive
  number (decimal, exponent, `0x`/`0o`/`0b`) and treats anything else (empty, garbage, `NaN`,
  `Infinity`, `1_0`, zero, negatives) as 0, the first poll. The cursor of an empty answer is that
  integer.
- **Why.** Today's route did `Number(since)` and fell back to 0; a typed `int` parameter would turn
  a stale or odd cursor into a 400 the poller does not expect.
- **Evidence.** `tests/runs/test_api.py::test_live_pages_with_since`.

## 6. A request body that is not JSON gets ninja's own 400 sentence (handoff to `core.errors`)

- **What.** `POST /api/approvals/{id}/decision` with a body that is not JSON answers
  `400 {ok:false, message:"Cannot parse request body"}`: ninja raises `HttpError(400, …)` before
  validation, and `core.errors.on_http_error` passes its message through. Readable bodies that
  fail validation get "That answer could not be read." as specified.
- **Suggested fix (step 1's file).** In `errors.install`, map ninja's body-parse `HttpError(400)`
  to the running operation's `invalid` message. The browser always sends JSON, so nothing in the
  UI depends on it. `tests/runs/test_api.py::test_decision_endpoint_refusals` asserts only the
  status and `ok:false` for that case.

## 7. Commands

- **`manage.py worker [--shutdown-timeout N]`** defaults to 60 s. It exits 1 when a lane stops on
  its own (a side task failed, e.g. the database went away), after stopping the others. The
  startup sequence and the three lanes are in `serve()`, which the tests drive with a stop event.
- **`manage.py debug_sleep <seconds>`** is gated on `settings.DEBUG` (as `dev_session` is) and prints
  `job: <id>`. The `debug.sleep` task itself is always registered (system lane, retry 0).
- **`manage.py acceptance_checks [--since ISO]`**: the ledger line covers rows since `--since`
  (default: the latest successful import's `finished_at`, else every row); a user's `chat` row
  needs `conversation_id`, any other purpose needs `run_id`. "Doing past its timeout" dates a job
  by its latest procrastinate `started` event and uses `step.TASKS` (60 min for an unknown task).
  The terminal-event line also requires the event's kind to match the run's status.

## 8. Live checks run in this step (worker against `foray_app`)

- **Lock recovery, startup sweep.** `debug_sleep 300` (job 3, `doing`); `kill -9` at 00:35:00 UTC
  (last heartbeat 00:35:00.2); a second `debug_sleep 300` (job 6) waited `todo` behind
  `lock="debug"`. Restarted at 00:38:16: the startup sweep logged `failed stalled job 3
  (debug.sleep, lock debug)` and job 6 started at 00:38:18.
- **Lock recovery, next reap.** Job 9 `doing`; `kill -9` at 00:39:08 (heartbeat 00:39:02), job 12
  queued, restarted at 00:39:10: the sweep found 0 stalled (heartbeat 8 s old); the 00:40:00 reap
  failed job 9 and job 12 started at 00:40:00.5 (52 s after the kill).
- **`worker.online`** (`live.worker_status`, polled every 2 s): `True` until 00:35:44, `False`
  from 00:35:46, 45.8 s after the last heartbeat, with `lastSeenAt` still the crash-time heartbeat.
  After a SIGTERM the Workers unregister: `online=false, lastSeen=null` at once (§9.7).
- **`max_delay` "missed once".** After the 3 min 16 s stop, the restart deferred exactly one
  `maintenance.reap` and one `approvals.expire` (tick 00:38:00); the missed 00:36 and 00:37 ticks
  were not deferred, and `maintenance.prune` (last tick 03:17 the previous day, over 6 h ago) was
  not deferred. A worker restarted within the same minute deferred nothing twice.
- **`--shutdown-timeout 5`.** SIGTERM at 00:38:39.1 while job 6 slept: `stopping (running jobs get
  5.0s)`, job 6 `aborted`, `stopped` at 00:38:44.3, process gone.
- **Ping (worker side).** For a throwaway `create_test_user` user, a `worker.ping` job deferred as
  the endpoint does was answered in 0.07 s: the event `{jobId: 15, workerPid: 95869}` (the
  worker's pid), message "The worker answered a test job from Settings.", read back through
  `live.snapshot`. The user was purged afterwards.
- The remaining §15.4 rows (`attempts` 0 then 1, `procrastinate_cancel_job_v1(id, abort,
  delete_job)`, the stalled-jobs API, the shutdown-only retry) are checked by
  `tests/runs/test_tasks.py` and `test_reaper.py` against real Workers on the test database.
- **Startup sweep test (fix 1).** `test_reaper.py::test_startup_sweep_fails_a_stalled_job` patches
  `periodic.install` to install nothing, so no `maintenance.reap` can fail the stalled job and
  only `serve()`'s sweep can release `lock="debug"`; it also asserts no reap job exists. Its
  watcher sets the stop event in a `finally` and `serve()` sits under `asyncio.wait_for(…, 60)`,
  so a missing sweep fails the test instead of hanging it. Mutation check: with
  `swept = await reaper.fail_stalled_jobs()` replaced by `swept: list[int] = []` the test failed
  in 20.4 s ("timed out waiting for …"); restored, it passes in 0.3 s.
- Legacy `foray` write counters stayed at 41158/1014040/5351.
- **Deferred to integration:** `POST :3000/api/worker/ping` with `pnpm dev:all`.

## 9. Observations, no change

- `tests/accounts/test_commands.py::test_create_test_user_purge` failed once in this lane's
  baseline full run (before any step 4 change) and passed alone and in every later full run.
  Not this step's file. **Corrected by step H1:** the cause stated here first ("an
  order-dependent count of `@example.test` users") was wrong; the test does not depend on order
  or on other tests' users. It compared the sorted listing of `STORAGE_DIR` with the *unsorted*
  list `[keep.pk, "unrelated.txt"]`, so it failed whenever the kept user's random id sorted after
  `unrelated.txt` (an id starting `v`–`z`, about 8% of runs), as step-3F.md §10 found.
  Integration G2 fixed it by sorting both sides (integration-G2.md §1); step H1 confirmed the
  fix and ran the test 30 times (deviations/step-H1.md §4).
- The dev `.env` sets `WORKER_CONCURRENCY=4`, so the background lane ran 4 wide here.
