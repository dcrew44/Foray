# Group G2 integration: deviations and clarifications

Later steps read this before the design. Each entry: what, why, evidence.

G2 is steps 3F and 4. Each lane's single commit was cherry-picked onto
`claude/middleware-django-migration-389b1a` in that order:

- 3F: `8f5f703` became `f5de6f9`;
- 4: `1ffd612` became `9b2acc7`.

**There were no conflicts.** The lanes own disjoint files (`apps/legacy/**` with
`tests/legacy/**`, and `apps/runs/**` with `tests/runs/**`), so there was nothing to resolve.
Combining the lanes broke nothing, and no lane file needed a change to make them work together.

## 1. Handoff applied: the flaky purge test compares sorted lists

- **What.** `backend/tests/accounts/test_commands.py::test_create_test_user_purge` (step 3A's
  file) now compares `sorted(path.name for path in storage.iterdir())` with
  `sorted([keep.pk, "unrelated.txt"])`. Before, it compared with the unsorted list.
- **Why.** This is the handoff from steps 3F (deviations/step-3F.md §10) and 4 (step-4.md §9).
  The kept user's folder is named after its random 32-character id. Whenever that id sorts after
  `unrelated.txt` (one starting `v`–`z`), the sorted listing puts `unrelated.txt` first and the
  test fails. That happens in about 8% of runs. It happened in the integration's first full run,
  so the combined suite was not green until the test was fixed. The command under test was not
  changed.
- **Evidence.**
  - Before the fix, the first full run gave 1 failed, 1302 passed: `['unrelated.t…AFf0lTe2xJZo']`
    against `['xljxjGha5na…nrelated.txt']`.
  - After the fix, the test passed 25 times out of 25.
  - It also passed under a scratch pytest plugin (since deleted) that forced every new user id
    to start with `z`.
  - The full suite then passed: 1303 passed.

## 2. The deferred step 4 checks, through `:3000` with `pnpm dev:all`

The checks used a `create_test_user` user and a `pnpm dev:session` cookie. Unsafe requests sent
`Origin: http://localhost:3000`.

- **Ping.** `POST :3000/api/worker/ping` answered `200 {"ok":true,"jobId":46}`. Polling
  `:3000/api/live?since=<cursor>` showed the `worker.ping` event after 0.085 s, with
  `{"jobId":46,"workerPid":3564}` and the message "The worker answered a test job from
  Settings.". Pid 3564 is the pid the worker logged as `ready (pid 3564)`. Two repeats took
  0.084 s and 0.080 s (jobs 47 and 48).
- **Refusals through the rewrite.**
  - No cookie: 401 "Not signed in.".
  - A cookie but no Origin: 403 "Cross-site request refused.".
  - A cookie with `Origin: http://evil.example`: 403 "Cross-site request refused.".
- **Offline after a crash.** The worker's last heartbeat was 01:24:44.964. It got `kill -9` at
  01:24:50.5, and from then on `:3000/api/live` was polled every 2 s.
  - It read `online:true` until the poll at 01:25:31.098 read
    `{"online":false,"lastSeenAt":"2026-10-09T01:24:44.964Z","pid":null}`. That is 46.1 s after
    the last heartbeat and 40.6 s after the kill.
  - `lastSeenAt` stayed at the crash-time heartbeat, because the three dead
    `procrastinate_workers` rows stay (§9.7).
- **Restart and graceful stop.**
  - A fresh `pnpm dev:worker` started at 01:25:53. It pruned the dead rows: rows 37–39 were
    replaced by rows 40–42. `:3000/api/live` read `online:true` at 01:25:53.488, and a ping
    answered in 0.076 s with `workerPid` 3801, the new worker's pid.
  - SIGINT went to its watchfiles process at 01:25:59.6. The log showed `stopping (running jobs
    get 5.0s)` and then `stopped` at 01:25:59.762.
  - `:3000/api/live` then read `{"online":false,"lastSeenAt":null,"pid":null}`, with 0
    `procrastinate_workers` rows. This is the declared change of §9.7.
- The lane's own handoff said `foray_app` held three dead worker rows (ids 31–33, heartbeat
  01:15:09.9). The integration's first worker start pruned them, as the handoff said it would.

## 3. Observations, no change

- **`dev:worker` does not restart a crashed worker.** After `kill -9`, watchfiles leaves the
  worker as a zombie (`<defunct>`) and waits for a file change. On SIGINT it logs "process already
  dead, exit code: -9". concurrently keeps web and api running when the worker exits, because
  `dev:all` has no `--kill-others`. So under `pnpm dev:all`, a crashed worker comes back only
  when a `.py` file is saved or `dev:all` is restarted. Step 5C's kill-during-a-run check has to
  start the worker again itself. Here, `pnpm dev:worker` was started on its own.
- **The access log hides `/api/live`.** A successful `/api/live` poll writes no access-log line,
  because step 1's filter removes it (step-1.md §14). Refused polls are still logged
  (`Unauthorized: /api/live`).
- **`backend/openapi.json` and `schema.d.ts` stay step 3G's stub** (integration-G1.md §3). The new
  `/api/live`, `/api/runs/*`, `/api/approvals/*` and `/api/worker/ping` operations are not in
  them yet. Step 7 regenerates them, and until then `pnpm check`'s final `git diff` step fails.
- **State of `foray_app` afterwards.**
  - 0 users and 0 `django_session` rows. `create_test_user --purge` printed "Deleted 1 test user.",
    and the purge left 1 orphaned session, which the integration then deleted.
  - 0 `procrastinate_workers` rows.
  - `procrastinate_jobs`: 39 succeeded, 6 failed and 6 aborted. That includes lane 4's
    `debug.sleep` jobs.

## 4. Handoffs not applied (still open)

- `apps/core/errors.py` (step 1's): map ninja's body-parse `HttpError(400, "Cannot parse request
  body")` to the running operation's `invalid` message (step-4.md §6). This is not a
  combination problem, and nothing fails on it.
- `apps/core/jsonio.py` (step 1's): the 10 strict xfails in `tests/llm/test_jsonio.py`
  (step-3B.md §9).
- The open handoffs of steps 3F and 4 for 5A, 5C, 6 and 12 stand as written in step-4.md §§1, 2
  and 4 and step-3F.md §§6 and 7.

## 5. Checks run on the combined code

- **Setup.** `:3000` and `:8000` were free. `uv sync`, `pnpm install --frozen-lockfile
  --prefer-offline` and `pnpm db:migrate` all ran; there were no migrations to apply.
- **pytest.**
  - Final full run: 1303 passed, 7 skipped and 10 xfailed.
  - The 7 skips are:
    - R7, R8, R13, R15 and R17 pending;
    - `apps/chat/tools.py` not landed;
    - the profile page.
  - The 10 xfails are the strict jsonio ones.
  - `tests/legacy` and `tests/runs` together gave 151 passed. The render tests ran on local
    Chrome and the contract tests ran in Node.
- **Static checks.** `ruff check` passed, and `ruff format --check` found 230 files already
  formatted. pyright gave 0 errors. `tsc --noEmit` and `eslint` both exited 0.
- **Live checks.** See §2. No step in G2 has a real-model Done check, so $0 was spent.
- **Legacy `foray`.** The write counters were 41158/1014040/5351 before and after.
  `git -C <main checkout> status --porcelain` was `?? reports/` and `?? research_notes/` before and
  after.
- **Processes.** Every server and worker that was started was stopped:
  - no `concurrently`, `next dev`, `uvicorn`, `watchfiles` or `manage.py worker` process remains;
  - `:3000` and `:8000` are free.
