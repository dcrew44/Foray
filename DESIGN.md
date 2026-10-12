# Foray — design

Foray is an AI-first job search product. You onboard with a resume, answer a short
interview, then say things like *"I want entry-level software engineering jobs in the
Bay Area"* and agents do the work in the background: search many sources, rank every
posting against your profile, tailor your resume per job, and — in later phases — apply
through a browser and email recruiters.

This document records **what is settled and why**. The step-by-step build plan lives in
`~/.claude/plans/i-no-longer-want-luminous-kay.md`; progress is tracked by commits on
`main`.

The previous incarnation of this repo — a Python MCP-server learning prototype — is
preserved under the git tag `python-prototype`. The TypeScript server of Phase 1 (Next.js
route handlers and server actions, a Node worker on pg-boss, Drizzle, Better Auth) was
replaced in October 2026 by the Django backend this document describes; the last
pre-migration commit of `main` still runs it against the untouched `foray` database.

## Settled decisions

| Topic | Decision |
|---|---|
| Audience | Single user now, **structured for SaaS**: auth, `user_id` on every domain row, user-scoped queries from day one |
| Autonomy | **Per-run toggle**: `approve_each` (default) or `autopilot`; approvals surface in chat and in the activity feed |
| Stack | **Django backend, Next.js frontend.** Python 3.14: Django 6.1 with django-ninja 1.7 (the JSON API, sessions, the ORM) on uvicorn, and one procrastinate 3.10 worker process. Next.js 16 only renders: server components read one Django page endpoint each, client components call Django through a typed client, and `/api/*` is rewritten to Django. Types flow from Django's OpenAPI schema into TypeScript (`pnpm api:types`) |
| LLM | **Multi-provider, no default vendor.** pydantic-ai 2.54 models (OpenAI, Google, DeepSeek, Anthropic, plus an OpenAI-compatible slot); the model for each *purpose* is an env setting, and `LLM_DEFAULT` is required at boot |
| Fetching | **Hybrid: code fetches, agents decide where.** Deterministic adapters (Greenhouse, Lever, Ashby, JSearch) + an agent-managed company registry + an LLM extraction fallback for career pages, all through one guarded HTTP client |
| Browser | Phase 1 uses headless Chromium (Playwright for Python, in the worker) only to render PDFs. Phase 2 adds a Playwright persistent profile per user behind a `BrowserProvider` interface |
| Outreach | Gmail OAuth send + reply tracking; Apollo/Hunter for recruiter emails (Phase 3) |
| Hosting | **Local first, cloud-ready.** Four processes, the same in dev and prod: Postgres, Next, Django (api), the worker. Postgres runs in Docker Compose; web, api and worker run natively in dev (`pnpm dev:all`); one `Dockerfile` (a `backend` image for api, worker and migrate, a `web` image) and a `prod` compose profile run the same containers |
| Resume output | Structured resume → **PDF via HTML template**; a renderer interface can be added when a LaTeX (tectonic) renderer is compared |
| Budget | ~$100/month of third-party APIs; model calls stop for the month at `LLM_MONTHLY_BUDGET_USD` (default $90) of ledger spend |
| Workflow | Build autonomously per phase, **commit each working step to `main`**, user tries it in the browser and steers |

## Invariants

These are the rules that must survive every later change. Where rows of the invariant
register below enforce and prove one, it names them.

1. **Everything intelligent runs server-side.** Django serves chat turns
   (`POST /api/chat`); the worker runs background pipelines. Next renders and the browser
   streams. Provider API keys and the job mirror never leave the server: Settings shows key
   *presence* only, and no endpoint returns `jobs.raw`.
2. **Job id = `source:company_slug:external_id`.** Career-page postings use
   `web:<company_slug>:<sha1(url)>`, with `url` resolved by `ada_url` (Node's own URL parser,
   so ids stay stable across the port). Ids are stable across refetches.
3. **Adapters normalize; models hold data.** No source-specific branching outside
   `backend/apps/jobs/sources/<source>.py`.
4. **Store the raw description untouched; clean separately.** `description_raw` is
   never rewritten: it changes only when the source's own posting changed.
   `description_text` is the cleaned copy. (One documented exception: a careers-page posting
   stores its page *text* as `description_raw`, because whole HTML pages would bloat the
   table for no reader.)
5. **Strip boilerplate only by known patterns** (EEO, E-Verify, pay transparency) and
   only in the trailing 30 % of a posting. Never summarize a description into storage.
6. **`fetched_at` is cache metadata, not a job fact.** Job facts are `posted_at`,
   `first_seen_at`, `last_seen_at`.
7. **Postings that vanish from a source are kept with `is_active = false`.** The mirror
   is history, not a snapshot. Only a complete, successful board fetch deactivates, and only
   a second consecutive empty answer closes a company's postings (the empty-board guard).
   Careers pages and the aggregator never deactivate.
8. **Per-user rows are reachable only through `for_user(user_id)`** (or an explicit,
   reasoned `unscoped(reason)` in system code). There is no unscoped read path to per-user
   data. *Register: R8.*
9. **Call sites ask for a purpose, never a model name** (`model("score")`,
   `structured("score", …)`). Vendor quirks live in `backend/apps/llm/reasoning.py` only.
10. **Every model request writes an `llm_calls` ledger row** (provider, model, purpose,
    tokens, cached tokens, USD, latency, user, run, conversation): success, vendor error,
    timeout, cancel and stream alike. This is a wrapper on the model itself
    (`apps/llm/ledger.py` `LedgerModel`), not discipline at call sites, so agent-internal
    calls are recorded too. Attribution to a user, run or conversation travels in a
    contextvar (`apps/llm/context.py` `llm_context`). *Register: R10, R12.*
11. **Import boundaries.** The frontend imports no server module: Next reaches the backend
    only over HTTP, through `src/lib/api/server.ts` in server components and
    `src/lib/api/client.ts` in client components, and a `"use client"` module never imports
    `server.ts` (ESLint). In the backend, async code never touches the ORM and sync code never
    awaits: the one bridge is `apps.core.db.db()`. `tests/test_register.py` keeps the
    conventions a type checker cannot see (one HTTP client, one defer path, one router
    factory, no `auto_now`). *Register: R9.*
12. **Nothing heavy runs inside a request.** Model calls, third-party fetches and PDF
    rendering run in the worker as runs (uploads, adding a company and refreshing one
    included). The one exception is the chat stream, capped at 8 steps, a 180 s turn and a
    per-user throttle.
13. **One set of resume facts.** The latest base `resumes` row
    (`apps.resumes.queries.latest_base_resume`) is what scoring, deep review, tailoring and
    the chat agent read. Editor saves create a new base version; base versions are unique.
    There is no second copy to drift. *Register: R14.*
14. **One role, one match.** The prefilter collapses postings to one per role (company +
    normalized title, preferring the company's own board and the newest copy) and skips
    roles the user already has a match for. The ingest dedupe key keeps locations apart
    because the mirror should; a person does not want the same role four times.
15. **No silent re-spend, no zombie runs.** A run row and its first job commit together;
    every job that works on a run holds the lock `run:<id>`, and a minutely reaper fails
    any queued or running run that no live job holds. Tasks that can call a model never
    retry, and a stalled job is failed, never retried. A user can always re-run a search,
    and because scoring and deep review skip finished work, a re-run resumes rather than
    repaying. *Register: R1, R2, R3, R4, R11.*
16. **A search follows its request.** Each run reads what was asked into a brief
    (`apps/matching/brief.py`): companies, roles, level, places, remote, focus. The brief
    overrides the saved preferences for that run only (`apply_brief(saved, brief)`), and
    filtering, scoring, deep review and the results all read the effective preferences. A
    search's deep-review batch and its results come only from its own postings. (Before
    this, the request was stored "for the record": every search ran on the saved
    preferences, returned the same roles, and topped up its review with leftovers from
    earlier searches.) *Register: R15.*
17. **Work started in a chat reports back in that chat.** When a run with a
    `conversation` pauses for a decision, finishes or fails, `emit()` defers a
    `chat.follow_up` job in the same transaction; it posts an automatic update to that
    conversation and has the chat agent answer it (`apps/chat/follow_up.py`). The follow-up
    turn has read-only tools, never lands in the middle of a reply the user is still
    receiving, posts at most once per event, and skips news that is already stale (a
    decision answered from the card, a cancelled run). Every run kind ends with
    `run.completed`, `run.failed` or `run.cancelled`, so this covers every kind.
    *Register: R7, R17.*

## Invariant register

The rules the backend implements, each with what enforces it and the tests that prove it.
Test paths are relative to `backend/tests/`. `tests/test_register.py` parses this table and
fails if a named test does not exist (with `FORAY_REGISTER_STRICT=1`, a missing test *file*
fails too), so a row cannot outlive its proof.

| # | Invariant | Enforced by | Proved by |
|---|---|---|---|
| R1 | A run row and its first job commit together, or neither does, in every process | `runs.lifecycle.start_run` (one transaction, savepoint insert, `defer_in_tx` on Django's connection); `test_register` bans every other defer | `runs/test_lifecycle.py::test_start_is_atomic`, `runs/test_defer_in_tx.py::test_rollback_leaves_no_job_under_worker_connector` |
| R2 | At most one active run per `(user, dedupe_key)` | partial unique index `runs_one_active_per_key` | `runs/test_lifecycle.py::test_one_active_run_per_key_under_concurrency` |
| R3 | An active run (`queued`, `running`) is always held by a `todo`/`doing` job with `lock = 'run:<id>'`. Otherwise the reaper fails it within 2 minutes. An `awaiting_approval` run with no pending approval and no live job is failed after 10 minutes | `run_step` lock convention; `runs.reaper.reap()` | `runs/test_reaper.py::test_reaper_fails_orphaned_runs`, `::test_reaper_spares_live_continuation`, `::test_reaper_fails_lost_continuation` |
| R4 | A run finishes only through `complete()`, `fail()` or `cancel()`. Each is conditional on an active status and emits exactly one terminal event, which is the run's last event | `runs.lifecycle` | `runs/test_lifecycle.py::test_one_terminal_event_per_run`, `::test_cancel_beats_late_complete`, `::test_handler_that_forgets_to_finish_fails` |
| R5 | Within one user, event ids become visible in id order, so `?since=` never skips | `runs.events.emit()` takes a per-user advisory xact lock and is the last statement of its transaction | `runs/test_live_cursor.py::test_cursor_never_skips_out_of_order_commits` |
| R6 | An approval is decided exactly once. The decision, the run transition, the event and the continuation job commit together, in every process | `runs.approvals.decide()`, `request_approval()` and `expire_lapsed()` (`select_for_update`, `defer_in_tx`) | `runs/test_approvals.py::test_concurrent_decides_one_wins`, `::test_decide_rollback_leaves_pending`, `runs/test_defer_in_tx.py::test_rollback_leaves_no_job_under_worker_connector` |
| R7 | A follow-up posts at most once per event, is never deferred before its event commits, and never interleaves with a streaming user turn | unique `(conversation, run_update_event)`; queueing lock `follow-up:<event>`; `emit()` defers through `defer_in_tx`; marker and `updated_at` check | `chat/test_follow_up_db.py::test_follow_up_once`, `::test_follow_up_yields_to_user_turn`, `runs/test_defer_in_tx.py::test_rollback_leaves_no_job_under_worker_connector` |
| R8 | Per-user rows are reachable only through `for_user(uid)` or an explicit `unscoped(reason)` | `core.scoping.ScopedManager`; grep allowlist | `core/test_scoping.py::test_scoped_manager_refuses_unscoped`, `pages/test_tenancy.py::test_tenancy_sweep` |
| R9 | No ORM use on the event loop, and no LLM call inside a transaction | `core.db.db()` (sync-only functions); Django's `SynchronousOnlyOperation` | `core/test_db.py::test_db_rejects_coroutines`, `::test_db_returns_connection` |
| R10 | Every model request writes a ledger row: success, vendor error, timeout, client cancel, stream | `llm.ledger.LedgerModel` | `llm/test_ledger.py::test_ledger_matrix` |
| R11 | No silent re-spend: every task that can call a model has `retry=0`, and stalled jobs are failed, never retried | task registry; `runs.reaper` | `runs/test_tasks.py::test_money_tasks_never_retry` |
| R12 | Monthly model spend is bounded by `LLM_MONTHLY_BUDGET_USD` | `llm.budget.BudgetGuard` (outermost wrapper) | `llm/test_budget.py::test_budget_guard_refuses` |
| R13 | Streams never pin or leak pooled connections | chat view releases before streaming; all generator DB work via `db()`; generator `except`/`finally` | `chat/test_chat_view.py::test_stream_errors_leak_nothing`, `::test_live_answers_while_streams_open` |
| R14 | One source of resume facts: the latest base resume. Base versions are unique | partial unique constraint; `resumes.queries.latest_base_resume` | `resumes/test_edits.py::test_base_versions_unique_under_race` |
| R15 | A search follows its request (`apply_brief`) | `matching.brief.apply_brief` used by every search reader | `matching/test_brief.py` (ported), `matching/test_search_pipeline.py::test_brief_overrides_saved_preferences` |
| R16 | The legacy database and the legacy file tree are never modified, and no Django process ever writes to a legacy-looking database | `legacy.importer` (read-only session, copy-only files); `env.py` name check and `core.guards.assert_app_database` | `legacy/test_import.py::test_import_leaves_legacy_untouched`, `core/test_guards.py::test_migrate_refuses_legacy_target`, `legacy/test_import.py::test_refuses_swapped_urls` |
| R17 | Every run kind ends with `run.*`, and `FOLLOW_UP_KINDS` is the same set in Python and TS | `runs.lifecycle`; generated constant check | `runs/test_lifecycle.py::test_every_kind_finishes_with_run_event`, `chat/test_follow_up_db.py::test_follow_up_kinds_match_frontend` |

`manage.py acceptance_checks` re-checks the database-level rows (R3, R4, R10, and the ledger's
attribution) against live data after a browser rehearsal. Its run-level lines cover only runs
created after the legacy import, because imported history is kept as it was.

## Architecture

```
Browser ─▶ Next.js 16 on :3000  (renders pages; /api/* is rewritten to Django)
              │ server components: one page read each   │ client components: typed fetch + router.refresh()
              ▼                                          ▼
          Django 6.1 on uvicorn :8000  (django-ninja JSON API, sessions, POST /api/chat stream)
              │ start_run: the run row and its first job in one transaction    ▲ reads runs / events /
              ▼                                                                │ approvals / matches
          Postgres 18, database foray_app  (app tables + procrastinate's) ◀────┤
              ▲ LISTEN/NOTIFY, fetch, heartbeat                                │
          Worker (manage.py worker): procrastinate in three lanes; source fetchers + discovery,
                 LLM pipelines (pydantic-ai), headless Chromium PDF renderer (Playwright)
```

There is no Redis, no Caddy and no push hub: four processes, the same in dev and prod.

- **Ten rules remove most of the design surface.**
  1. Requests never wait on a model or a third-party site, except the chat stream.
  2. A run is the only unit of background work a user sees, and every kind ends with
     `run.completed`, `run.failed` or `run.cancelled`.
  3. **One way to start work:** `runs.lifecycle.start_run` inserts the run and defers its
     first job in one transaction. Every defer goes through `apps.core.queue.defer_in_tx()`,
     which writes the job on Django's own connection, in every process, the worker included.
  4. **One way to dedupe:** `runs.dedupe_key` under a partial unique index on active runs.
  5. **One way to recover:** every job that works on a run holds the procrastinate `lock`
     `run:<id>`, and a minutely reaper fails any active run that no live job holds.
  6. **Sync Django for the database, async only at network edges,** bridged by `db()`.
  7. **Polling stays,** with a commit-safe cursor.
  8. **Chat stores AI SDK UI messages only;** model history is derived from them each turn.
  9. **Django defaults for auth.** Session cookies (`sessionid`, SameSite=Lax) plus an
     Origin/Fetch-Metadata check replace CSRF tokens. Better Auth's scrypt hashes, carried over
     by the import, are verified once and upgraded to PBKDF2 on that sign-in.
  10. **Next renders.** Server components read one page endpoint each (`/api/pages/<page>`);
      client components call Django directly, then `router.refresh()`.
- **Backend layout.** One Django project (`backend/foray`: settings, the fail-fast `env.py`,
  middleware, the ninja API) and ten apps under `backend/apps`: `core` (the `db()` bridge,
  `defer_in_tx`, scoping, the error envelope, the router factory, the guarded HTTP client,
  storage, throttles), `accounts` (users, auth, preferences and settings), `llm`, `jobs`
  (sources, ingest, the company registry, discovery, refresh), `matching` (brief, prefilter,
  fast score, deep review, the search pipeline), `resumes` (upload, extraction, edits,
  tailoring, verification, rendering), `runs` (the kind catalogue, lifecycle, events,
  approvals, the live snapshot, the reaper, the worker command), `chat`, `pages` (one
  read-only view model per Next page) and `legacy` (the one-off import from the TypeScript
  app's database).
- **Queue**: procrastinate on Postgres (no Redis), with LISTEN/NOTIFY so a job starts the
  moment its transaction commits. The worker runs three lanes in one process: `interactive`
  (imports, tailoring, rendering, single evaluations, adding companies, chat follow-ups),
  `background` (searches and their continuations, board refreshes, the six-hourly refresh of
  stale boards) and `system` (the reaper, approval expiry, pruning, the Settings ping), so a
  long search never blocks a tailoring job the user is waiting on, and liveness never queues
  behind model work. A step is a `run_step` task: it claims the run, runs under the run's
  `llm_context` and a per-task timeout, turns a failure into `run.failed` with the user's
  sentence, and fails a run its handler forgot to finish.
- **Progress, one channel**: an append-only `events` table, written only by
  `runs.events.emit()`. The app shell's `LiveProvider` polls a single endpoint,
  `/api/live?since=<id>`, which returns new events, active and recently finished runs,
  pending approvals and the worker's status in one read. Every run card, approval card,
  sidebar badge and toast reads from that one snapshot, so they always agree, and each
  outcome toasts once. Cadence adapts: 1.5 s while work is in flight, 6 s idle, 30 s in a
  hidden tab. `emit()` takes a per-user advisory lock and is the last statement of its
  transaction, so ids become visible in id order and the cursor never skips an event.
- **Worker liveness** comes from procrastinate's own heartbeat: the sidebar says "Worker
  offline" once the newest heartbeat is 45 s old (computed in SQL, so clock skew cannot fake
  it), because a queued search with no worker is otherwise indistinguishable from a slow one.
  A lane that loses the database (a Postgres restart) is started again with a backoff instead
  of ending the process, and the prod containers restart unless stopped by hand.
- **Agents**: a pydantic-ai `Agent` drives chat, built per turn with the chat purpose's model
  stack and 16 tools, and streamed to the browser in the AI SDK's UI-message protocol, which
  `useChat` reads unchanged. Search / rank / tailor / discovery are plain procrastinate
  pipelines with approval gates, not agent loops. Browser-driving agent loops arrive in
  Phase 2.
- **Follow-ups**: `emit()` defers a `chat.follow_up` job for the moments a run reports
  (`approval.requested`, `run.completed`, `run.failed`) when the run came from a chat. The
  job waits for any streaming user turn to end, then stores an automatic update (a user-role
  message marked `run_update`, drawn as a divider) together with the agent's reply, and emits
  `chat.followup` so an open chat merges it in; the app toasts it anywhere else.
- **Approval gate pattern** (the durable-workflow substitute reused by every later phase): a
  step that reaches a gate writes an `approvals` row naming its continuation task, emits
  `approval.requested`, sets the run to `awaiting_approval`, and returns. Deciding — from the
  UI or a chat tool — moves the run back to `running` and defers the continuation in the same
  transaction, so a decision and its job commit together or not at all. In `autopilot` the
  same path auto-approves with `decided_via: 'autopilot'`. Unanswered approvals expire 7 days
  later through a per-minute sweep.
- **The one async rule.** Ninja operations are sync (each request runs in its own thread);
  the chat view is the one async endpoint. Async code reaches the database only through
  `await db(fn, …)`, which runs a sync function on a pooled thread and returns its connection,
  so a stream never pins a connection, and a model call (async) can never happen inside a
  transaction (sync). Fan-out (16 fast scores, 5 deep reviews, 6 board fetches) is an
  `asyncio.Semaphore` plus a `TaskGroup`.
- **Auth and requests.** Every endpoint is authenticated except sign-in, sign-up, the auth
  config and health. Expected failures are `{ok: false, message}` with a 4xx and a sentence a
  person can read (never raw validation text); not signed in is `401`, never a redirect.
  Per-user throttles live in a Postgres table (chat turns, uploads, company adds,
  evaluations, tailoring, sign-in failures). Wire keys are camelCase from snake_case Python
  (every router comes from `core.router.router()`, which serializes by alias).
- **Types across the boundary.** `pnpm api:types` exports Django's OpenAPI schema
  (`backend/openapi.json`) and generates `src/lib/api/schema.d.ts`; `pnpm check` fails when
  either is stale, so a response change cannot land without the frontend type-checking against
  it.

### Data model, in two layers

- **Shared mirror** (`companies`, `jobs`): one continuously refreshed copy of every
  posting pulled from any source, shared by all users. One fetch serves everyone.
- **Per-user layer** (everything else): `users` itself (which holds preferences, settings —
  default autonomy, score threshold, deep-review count — interview state and time zone; there
  is no separate profile row that might be missing), then `documents`, `resumes`,
  `job_matches`, `runs`, `events`, `approvals`, `conversations`, `messages` and `llm_calls`.
  Each of these carries `user_id` (a ledger row of system work has none) and inherits
  `UserOwned`, whose manager refuses any query that is not `for_user(...)` or
  `unscoped(reason)`.
- **Process state**: procrastinate's own tables (jobs, workers, periodic defers),
  `rate_limits`, `api_usage` (third-party request quotas by month) and `legacy_imports`.
- **Conventions**: Django migrations own the schema; plural table names; every timestamp is
  `timestamptz` and set explicitly (no `auto_now`, so imported rows keep their dates); the
  database is the one clock: a timestamp not given is `Now()` (`db_default`), updates write
  `Now()`/`now()`, and every comparison with "now" is made in SQL or against
  `core.clock.database_now()`, so a skew between the host's and the database's clocks cannot
  move the reaper's 60 s rule, the budget's month or a chat turn's marker; enums
  are `TextChoices` plus a database `CHECK`; JSON columns are read through lenient pydantic
  models and keep today's key spelling (camelCase in run and approval JSON and settings,
  snake_case in preferences and evaluations); new primary keys are UUIDv7.

### The search pipeline

A search is one run that moves through phases, pausing at two gates in `approve_each`:

1. read the request into a brief and title terms (cheap model, cached on the run);
   fields the chat agent already resolved win over what is read from the text
2. resolve named companies, registering a board the first time one is mentioned;
   refresh their boards (or every stale board for a broad search), query the
   aggregator unless every named company has a board (3 requests per run)
3. SQL prefilter over the mirror, limited to the named companies, one posting per role
4. fast score every new candidate (cheap model, triage), archive below the threshold
5. **gate: deep review** — the top N unreviewed matches among this run's postings, with
   a cost estimate from `apps/llm/pricing.py`; autopilot approves it automatically
6. deep review (strong model): requirement-by-requirement evidence quoted from the
   resume, gaps rated learnable or disqualifying, fit on level, location and pay
7. **gate: shortlist** — matches the review recommends; approving moves them to
   Shortlisted (phase `shortlisting`)
8. complete, with the best matches linked from the run card and the count of strong ones

Tailoring is separate and on demand: the strong model reorders, selects and rephrases
only what the base resume says; a verifier (deterministic rules plus a cheap-model
check) flags any claim it cannot trace to a base fact, unsupported lines are reverted
once automatically, and the result renders to PDF in the worker.

**PDF rendering** happens only in the worker: one Chromium per worker process, JavaScript
off and every network request aborted, Letter format, rendered normally and again compactly
if that saves a page. A render is reused by its `render_hash` instead of rendering again:

- a base resume's key is `sha256(html_normal | RENDER_VERSION)`;
- a tailored resume's key is `sha256(html_normal | RENDER_VERSION | "<Name> - Resume - <Company>.pdf")`,
  so a tailored PDF always downloads under its own name, even when tailoring changed nothing;

where `html_normal` is the normal-density HTML and `RENDER_VERSION` a constant in
`apps/resumes/render.py`, bumped whenever the template, the fitting rule or the PDF options
change. Byte dedupe cannot do this job, because every render carries a new `/CreationDate`.

## Interfaces

Defined now so later phases are additive, not a rewrite.

| Interface | Phase 1 | Later |
|---|---|---|
| Board adapters | `greenhouse`, `lever`, `ashby` share one protocol: `fetch_board(client, slug) -> list[NormalizedJob]`, raising `BoardNotFound` on a 404 | more boards |
| Other sources | `jsearch` and `careers_page` are plain functions with the same `NormalizedJob` output | — |
| `Storage` | `LocalStorage` (`apps/core/storage.py`: `put`, `open`, `delete`, `exists`) | S3 / Blob behind the same four methods |
| `ResumeRenderer` | future work: today there is one renderer, the HTML template in headless Chromium | `latex` via tectonic, compared side by side |
| `BrowserProvider` | future work (Phase 2) | local Playwright profile, then Browserbase |

## LLM layer

`apps/llm/registry.py` builds pydantic-ai models explicitly for `openai`, `google`,
`deepseek`, `anthropic` and an OpenAI-compatible `compat` slot, with keys passed from the
validated environment. There is **no vendor default in code**: `LLM_DEFAULT=<provider>:<model>`
is required at boot, and `LLM_CHAT`, `LLM_EXTRACT`, `LLM_SCORE`, `LLM_EVALUATE`, `LLM_TAILOR`,
`LLM_VERIFY`, `LLM_MERGE` override it per purpose (`extract` and `merge` need a
vision-capable model for the PDF-image fallback). A purpose never falls back to another
vendor, and every purpose's provider must have its key, or the process does not start.

Every call site gets the same stack for its purpose:

```
model(purpose) = BudgetGuard( RateLimited( LedgerModel( PurposeSettings( base_model(ref) ) ) ) )
```

- `PurposeSettings` applies the per-purpose reasoning effort from Foray's own map
  (`reasoning.py`, overridable with `LLM_REASONING_<PURPOSE>`; an invalid value fails boot).
- `LedgerModel` writes one `llm_calls` row per request, in a `finally`, for every outcome.
- `RateLimited` is a per-process token bucket per provider (`LLM_RPM_<PROVIDER>`).
- `BudgetGuard` refuses calls once the month's ledger spend reaches
  `LLM_MONTHLY_BUDGET_USD`; pipelines fail with that sentence and chat says it.

Structured output goes through `apps/llm/structured.py` (`structured(purpose, Output, …)`),
which retries malformed output once and never retries a truncated answer. `LLM_FAKE=1`
(development only) swaps every purpose for a deterministic fake, so whole flows rehearse at
$0.

Cost is a first-class concern: `apps/llm/pricing.py` holds per-`provider:model` rates
(including DeepSeek's off-peak bands), the ledger records every request, and
`pnpm llm:bakeoff` runs fixed fixtures across a list of models to print cost, latency,
schema-failure rate, and agreement with a reference model. Which models to use is settled
with those measurements, not guessed.

## Phase plan

- **Phase 1 — find, rank, tailor** (no submitting). **Complete**: onboarding (resume
  extraction, preferences interview or form), search across ~200 boards + JSearch,
  fast scoring, deep review and shortlisting with approval gates, the pipeline board,
  tailored resumes with a fact-checker, PDF rendering, usage and model settings. Rebuilt on
  the Django backend in October 2026 with the same behaviour, plus run cards for every slow
  action, a monthly spend guard and per-user throttles.
- **Phase 2 — browser agent + auto-apply.** `BrowserProvider`, `applications` and
  reusable `application_answers`, `apply` and `career_page_scan` run kinds, an
  `apply_submit` approval showing the filled form.
- **Phase 3 — outreach.** Gmail through Google OAuth (an `oauth_accounts` table with
  encrypted offline tokens, ID tokens verified against Google's JWKS; the "Continue with
  Google" sign-in returns with it), `contacts`, `outreach_messages`, `send_email` approvals.
- **Phase 4 — SaaS hardening.** Cloud deploy of the same containers behind a TLS front
  (`TRUSTED_PROXY_HOPS=1`, sign-up closed), Browserbase, S3 storage, usage billing off
  `llm_calls`, own company dataset, per-user company follows instead of one shared status,
  retention policy.

### Deferred: LinkedIn PDF import

Skipped by the user on 2026-09-18, after Step 8. It was to be the first consumer
of the provenance work: the same upload path with `kind: linkedin_pdf`, a
resume merge using the `merge` purpose, and a diff the user accepts, never
overwriting `source: user` items.

`linkedin_pdf` remains a `documents.kind` and an item `source`, and the `merge` purpose is
reserved for it. The upload endpoint takes no kind hint until the merge exists (the old
hint accepted any file and merged nothing). So this stays a small, additive piece of work
rather than a rewrite.

## Interface

The UI was rebuilt as its own piece of work at the end of Phase 1.

- **Shell**: a quiet sidebar on a lichen-grey canvas, and every page on a raised white
  sheet. Navigation: Home, Matches, Activity; Profile, Companies, Settings; recent chats.
- **Palette**: deep pine ink and primary, one signal colour (marigold) reserved for
  "agents working now" and "decisions waiting on you", brick only for errors. Match fit
  is one sequential pine scale (`--fit-0..5`), never traffic lights: the deeper the
  green, the better the match, so a board reads as a heat map.
- **Type**: Bricolage Grotesque for display (titles, headline sentences, scores),
  Instrument Sans for everything read or operated.
- **Dark by default.** The server renders dark; a script in `<head>` switches to light
  before first paint only for someone who chose Light (or Match system on a light OS).
  The choice lives in the browser (`src/lib/theme.ts`) and is set from Settings →
  Appearance or the account menu.
- **Chat renders tool calls as what they did**: a started run is a live run card (with
  its approval inline when it pauses), a job list is clickable match lines, a saved
  preference is one sentence. Raw JSON is one click away, never the default.
- **Slow actions are run cards**: uploading a resume, adding a company and refreshing a
  board show a live card, then one toast with the outcome.
- **Home is a status report**, not a greeting: the headline says what needs attention.

Roadmap items deliberately out of Phase 1: LinkedIn automation through the user's own
account, and a career-counselor onboarding mode (it adds its own column on `users` when it
is built; nothing is reserved for it).

## Known risks

| Risk | Mitigation |
|---|---|
| PDF parsing quality | text first (PDFium), vision fallback on garble, user corrects in the editor, fixtures across layouts |
| LLM extraction errors on career pages | used only where no API exists; posting URLs are always real links; unknown fields stay `unknown`; `needs_browser` flag for JS-rendered sites; unchanged listing pages reuse their cached extraction |
| Discovery false positives | a company is registered only after a live API call returns postings; a web-search result is used only when it is about the company; every step lands in the run's trail, which the add-company card shows |
| Cost per run | per-purpose model choice, per-purpose reasoning effort, skip-already-scored, score threshold, `deep_eval_top_n`, the deep-review gate's estimate, the monthly budget guard — all measured by the ledger |
| Model quality drift across vendors | vendor-neutral prompts with explicit schemas; the bake-off's schema-failure and agreement metrics |
| Rate limits | per-provider token bucket per process, `asyncio` semaphores per fan-out, JSearch request cap per run + monthly counter, per-user throttles |
| Cross-source duplicates | a normalized `dedupe_key` (company, title, primary location) at ingest, then the prefilter's one-role-one-match collapse (invariant 14) |
| Fetching what users and models type | one guarded HTTP client refuses private, loopback and metadata addresses on every request and redirect hop, caps response sizes, and sends a caller's API key only to the origin it was meant for |
| Data reachable from the network | Postgres publishes 5432 on loopback only and `next dev` listens on 127.0.0.1; `SIGNUP_OPEN=false` once the owner's account exists; every `.env.*` but the template is git-ignored |
| Prompt injection through postings | a posting's full text is wrapped in `<untrusted>`; short posting fields (titles, companies, headlines) reach the model unwrapped, follow-ups and a re-sent notice get read-only tools, and write tools have no structural confirmation yet |
| API drift vs. model training data | `AGENTS.md` and the vendored AI SDK skills for the frontend; pydantic-ai pinned exactly (it ships several times a week), Django and ninja pinned; golden and contract tests pin prompts, wire bodies and the UI-stream protocol |
| Tailoring fabrication | fact-grounded prompt + verifier with evidence ids + user approval in `approve_each` |
| Container/native drift | the same `env.py` validation and migrations in both; the containerized check is the guard |

## Corrections made with the Django migration

Statements this document used to make that were not true of the code, and what replaced them.

| # | It said | Now |
|---|---|---|
| D1 | Ambiguous company names become a `company_add` approval | Never built; removed here and from the approval kinds |
| D2 | `canonical_job_id` links cross-source duplicates | Never written; removed. The real mechanism is the `dedupe_key` plus the prefilter's one role, one match |
| D3 | "`call.ts` writes the ledger"; "`p-limit` per source" | The ledger is a wrapper on the model (invariant 10); fan-out uses `asyncio` semaphores |
| D4 | `LLM_DEFAULT` is required | It was not; now the process refuses to start without it |
| D5 | `JobSourceAdapter` covers JSearch and careers pages | Board adapters share a protocol; JSearch and careers pages are functions (Interfaces) |
| D6 | `ResumeRenderer` and `BrowserProvider` interfaces exist | Listed as future work; `Storage` exists |
| D7 | `profiles.assessment` is reserved | Dropped with the `profiles` table; the counsellor mode adds its own column |
| D8 | Invariant 12: nothing heavy in a request; chat capped "and a timeout" | True now: uploads and company adds and refreshes are runs, and a chat turn has a 180 s timeout |
| D9 | Invariant 13: `profiles.data` mirrors the latest base resume | The mirror is gone; the latest base resume is the only copy |
| D10 | Invariant 15 covers `running` runs but not `queued` ones | Atomic start, one active run per key and the lock-based reaper cover both |
