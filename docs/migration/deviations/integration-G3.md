# Group G3 integration: deviations and clarifications

Later steps read this before the design. Each entry: what, why, evidence.

G3 is steps 5A, 5B and 5C. Each lane's single commit was cherry-picked onto
`claude/middleware-django-migration-389b1a` in that order:

- 5A: `685be4b` became `55508d7`;
- 5B: `db926c7` became `0ab0814`;
- 5C: `7b44c32` became `9d6d732`.

**There were no conflicts.** The lanes own disjoint files (`apps/matching/**` with
`tests/matching/**`; `apps/resumes/{pipeline,tasks,api}.py`, `parse/sniff.py` and two test files;
`apps/jobs/{pipeline,tasks,api}.py` with `tests/jobs/test_pipeline.py`), so there was nothing to
resolve. Every check below passed on the combined code, and no lane file needed a change.

## 1. Duplicate OpenAPI component names: one, with identical shapes (5C's handoff, checked)

- **What.** A scratch script walked every operation's models and response models (nested fields
  included) and grouped the classes by component name. Only one name is claimed by more than one
  class: `OkMessageOut`, defined in `apps.accounts.api`, `apps.matching.api` and `apps.runs.api`.
  All three are `ApiModel` with `ok: bool, message: str`, and their `model_json_schema(by_alias=True)`
  are equal. So whichever one ninja's `dict.update` keeps, `openapi.json` gets the same
  component. No change.
- **Why it matters later.** A future edit to one copy would silently replace the others in
  `openapi.json`. Step 7, which regenerates the file, may move `OkMessageOut` to one shared module
  or add a test that fails on a component name with two different schemas.
- **Evidence.** "58 named models/enums reached from operations; DUPLICATE OkMessageOut
  ['apps.accounts.api…', 'apps.matching.api…', 'apps.runs.api…']"; the schema comparison printed
  `True`. A scratch `export_openapi_schema` lists 28 paths, including `/api/searches`,
  `/api/matches*`, `/api/companies*`, `/api/uploads`, `/api/documents/{id}`, `/api/resume*` and
  `/api/resumes/*`. The committed `openapi.json` and `schema.d.ts` stay step 3G's stub (§5.4).

## 2. How the deferred live checks ran

- **Two sessions of `pnpm dev:all`.** The first ran with `LLM_FAKE=1` ($0). The second ran with
  real models and `LLM_MONTHLY_BUDGET_USD=0.72` set in the process environment only, as a
  runaway backstop: the month's real spend before it ($0.007) plus both ceilings ($0.20 + $0.50).
  `.env` was not touched, and the guard never fired.
- **One throwaway user per lane and session:**
  - A (`--with-resume`): 5A on fakes;
  - B (`--with-resume --with-match`): 5B on fakes;
  - C (plain): the 5C checks;
  - A2 (`--with-resume`): 5A on real models;
  - B2 (`--with-resume --with-match`): the real tailor.

  The real runs used fresh users on purpose. A real search skips matches that already have a
  fast score ("Nothing new to score"), so A's 600 fake scores would have been reused. And B's
  base resume had become the fake extraction of `two-column.pdf`.
- **Order.** The 5C curls ran first, so the mirror was fresh when the searches read it. Then 5A
  and 5B on fakes, then the 5C kill, then the real session.

## 3. Observations, no change

- **The cron's catch-up ran live.** The worker's first start deferred
  `companies.refresh_scheduled` for the 00:00 UTC tick. That tick was 2 h 36 min old, under the
  6 h `max_delay` (§9.3, "missed once"). It logged "Refreshed 200/200 boards — 22603 postings, 11
  closed in 16.7s". The later starts (the hand restart in the kill check and the real session) did
  not defer it again. Before G3 the task was not registered, so step 4 skipped it.
- **Fakes do not cover the aggregator.** An unlimited search runs JSearch even on `LLM_FAKE=1`.
  The fake rehearsal made 3 real requests: `api_usage` shows `jsearch 2026-10 = 3` of the monthly
  200. A search whose named companies all have boards skips the aggregator, and the real run made
  none. Step 10's rehearsal should expect this.
- **`dev:all` after `kill -9`** behaves as integration-G2 §3 says: the worker is left `<defunct>`
  under watchfiles. It was restarted by hand with `pnpm dev:worker`, and later logged "process
  already dead, exit code: -9" on SIGINT.
- **`foray_app` after the checks:**
  - 0 users, 0 `django_session` rows. `create_test_user --purge` printed "Deleted 5 test
    users.", and the integration deleted the 5 orphaned sessions (decoded first: every one named a
    user that no longer exists).
  - 0 runs, no `todo`/`doing` jobs and 0 worker rows.
  - The ledger keeps 30 real rows ($0.149497) and 618 `fake` rows ($0) from this integration.
  - The kill check left 181 active boards stale (`last_fetched_at` NULL). The next six-hourly
    cron refreshes up to 200 of them. It is the dev mirror only.
- **The upload toast "Read 2 roles, 1 schools and 0 skills."** has no singular form. That is
  today's wording, verbatim (`src/components/profile/resume-dropzone.tsx:61`).
- **Lane test databases** remain on the server: `test_foray_app_lane5a`, `_lane5av`, `_lane5b`,
  `_lane5bv`, `_lane5c` and `_lane5cv`, plus earlier groups' copies. They are left for the
  orchestrator to drop.

## 4. Handoffs not applied (still open)

- **Two `resolve_match` implementations.** `apps.resumes.pipeline.resolve_match` (5B) and
  `apps.matching.queries.resolve_or_create_match` (5A) both exist now. Step 6 or 7 keeps one
  (deviations/step-5B.md §10).
- **Step 6 (chat tools)** uses the services of step-5A.md §8, step-5B.md §6 and step-5C.md §1 as
  written there. Note that 5C's `start_add` defaults to `refresh_after=True`.
- **Step 7 (pages).**
  - Regenerate `openapi.json` and `schema.d.ts`, now with every G3 operation.
  - If a page embeds `FastScore`/`DeepEval`, add `FastScore`, `DeepEval`, `MatchedRequirement`
    and `EvalGap` to `SNAKE_CASE_COMPONENTS` (step-5A.md §8).
  - Use 5C's `CompanyRow`/`row_for` for the companies page, and see §1 here on `OkMessageOut`.
- **Step 3C's `discovery.py`** (next serial owner): a pasted board link renames the company to its
  URL (step-5C.md §7).
- **Steps 8B and 8D (frontend)** follow the 202 shapes in step-5B.md §7 and step-5C.md §2.
- **Still open from G2:** the body-parse sentence in `apps/core/errors.py` (step-4.md §6) and the
  10 strict jsonio xfails (step-3B.md §9).

## 5. Checks run on the combined code

- **Setup.**
  - `git status` was clean, and `:3000` and `:8000` were free.
  - `uv sync`, `pnpm install --frozen-lockfile --prefer-offline` and `pnpm db:migrate` all ran,
    with no migrations to apply.
- **pytest.** The full suite gave 1522 passed, 6 skipped and 10 xfailed in 63 s.
  - The render tests ran on local Chrome, and the contract tests ran in Node.
  - The skips are R7, R8, R13 and R17 pending, `apps/chat/tools.py` not landed, and the profile
    page.
  - The xfails are the 10 strict jsonio ones.
  - `test_every_task_sits_on_its_lane` passes with all three lanes' tasks registered.
- **Static checks.** `ruff check` passed, and `ruff format --check` found 252 files already
  formatted. pyright gave 0 errors. `tsc --noEmit` and `eslint` both exited 0.
- **5C, through `:3000`** (user C, on fakes, $0):
  - `refresh-stale` (with 23 boards still stale after the cron) completed "Refreshed 23/23 boards
    — 4641 postings, 0 closed" with integer `boards` and `ok`.
  - `POST /api/companies {"query":"linear.app"}` answered 202. The run completed in 0.54 s with
    `registered:true` and `existed:true`, trail `[domain linear.app, careers-page
    https://linear.app/careers → ashby:linear]`. The events were `company.discovered` then
    `run.completed` "Linear was already on the list. Its Ashby board has 31 open roles.".
  - `{"query":"http://169.254.169.254"}` completed at level warn with `registered:false` and
    `company:null`. Its trail was `[domain, blocked "That address is not reachable from Foray.",
    slug-guess]`, and the events were `company.not-found` then `run.completed`. No company row was
    created.
  - `refresh-stale` right afterwards answered 202 with `queued:0`. It completed "None of your
    boards are stale." 0.17 s after the POST, emitting `company.refresh.done` then
    `run.completed`.
  - **Kill during a run.**
    1. `UPDATE companies SET last_fetched_at = NULL` touched 226 rows. `refresh-stale` then
       queued 200, and the run reached `running`/`refreshing`.
    2. The worker (pid 18301) got `kill -9` at 02:40:08.490 and became `<defunct>`.
    3. The hand restart was ready at 02:40:20.8; its sweep failed 0 jobs, because the heartbeat
       was too recent.
    4. The 02:41:00 reap failed stalled job 90 (`company.refresh`, `lock run:<id>`).
    5. The 02:42:00 reap failed the run with "Interrupted: the worker stopped while this was
       running. Start it again to retry.", 99.8 s after the restart. `run.failed` was its last
       event.
- **5A, through `:3000`, on fakes** (user A, $0):
  - `POST /api/searches {}` answered 202 and reached gate 1 (`awaiting_approval`/
    `awaiting_evaluation`) in 72 s, with 600 scored, 138 archived and 15 proposed.
  - A second `POST /api/searches` answered 409 "A search is already waiting on your go-ahead.
    Answer it on Home first." with the same `runId`.
  - The gate 1 decision `{"approved":true}` answered 200 "Approved. Deep-evaluating 15 roles in
    the background.". The run went back to `running`: `evaluation.start` and
    `evaluation.progress` are written only while the run is running.
  - Gate 2 was "Shortlist 6 roles". Its approval answered 200, and the run completed "Found 600
    roles, scored 600, deep-evaluated 15. 6 look worth applying to; 6 shortlisted."
  - The stages became `shortlisted` 6, `archived` 138 and `found` 456. Both approvals are
    `approved` with `decided_via` = `ui`.
- **5A, real models** (user A2, ceiling $0.50):
  - The prompt "Backend or software engineer roles at Reddit and Discord" refreshed those 2
    boards. The prefilter found 22 of 27,269 postings, and 7 were archived. Gate 1 came after
    10 s: "Deep-evaluate 15 roles (about $0.42)".
  - The gate 1 decision approved the top 5 `itemIds` (fast scores 92, 88, 86, 78, 78). It answered
    200, and the run was `running`/`evaluating` at once.
  - Evaluation was "Deep evaluation done: 5 worth applying.", and gate 2 was "Shortlist 5 roles".
    Its approval answered 200, and the run completed "Found 22 roles, scored 22, deep-evaluated 5.
    5 look worth applying to; 5 shortlisted."
  - **$0.118423 by the ledger:**
    - `score` (luna): 23 rows, $0.011879;
    - `evaluate` (terra): 5 rows, $0.106544.

    Every row carries the run's id.
  - The providers accepted DerivedSearch, FastScore and DeepEval as structured output.
- **5B, through `:3000`, on fakes** (user B, $0):
  - Uploading `two-column.pdf` answered 202 with `runId` and `documentId`. The `profile_import`
    run completed with `counts {work 2, education 1, skills 0, projects 0}`, `garbled:false`, and
    the message "Read 2 roles, 1 schools and 0 skills.".
  - An `MZ…PE` executable named `resume.pdf` answered 415 "Upload a PDF or a Word (.docx) file.".
  - Render answered 202, then completed with 1 page at density normal. `GET /api/documents/{id}`
    answered 200 with:
    - `application/pdf`, `Content-Length` 44918, and a body starting `%PDF-1.4`;
    - `inline; filename="…"; filename*=UTF-8''…` (and `attachment` with `?download=1`);
    - `private, no-store` and `nosniff`.
  - A second render returned the same `documentId` with `density:null`. The user still has one
    `resume_pdf`.
  - The fake tailor completed with a `documentId`, 0 changes and 25 lines checked.
  - The preview's headers through `:3000`: `Content-Security-Policy: default-src 'none';
    style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none';
    frame-ancestors 'self'`, plus `private, no-store`, `nosniff` and `Referrer-Policy:
    no-referrer`. Another user got 404.
- **5B, real tailor** (user B2, ceiling $0.20):
  - The match was the newest active job (Cloudflare). The run completed in 20.7 s with a new
    `documentId`: 3 changes, 26 lines checked, 0 unsupported.
  - **$0.031074 by the ledger:** `tailor` (terra) $0.030780 and `verify` (luna) $0.000294, both
    with the run's id.
  - The preview CSP was intact, and the PDF was served `inline` with the expected filename.
- **Totals.** Real spend was $0.149497 against a combined ceiling of $0.70. Fake rows: 618 at $0.
- **Legacy `foray`.**
  - The write counters were 41158/1014040/5351 before and after.
  - `git -C <main checkout> status --porcelain` was exactly `?? reports/` and `?? research_notes/`
    before and after. That is the read-only check the lanes' guard refused.
- **Processes.** Both sessions and the hand-restarted worker were stopped with SIGINT, and each
  worker logged `stopped`. No `concurrently`, `next dev`, `uvicorn`, `watchfiles` or
  `manage.py worker` process remains, and `:3000` and `:8000` are free.
