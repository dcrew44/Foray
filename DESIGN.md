# Foray — design

Foray is an AI-first job search product. You onboard with a resume, answer a short
interview, then say things like *"I want entry-level software engineering jobs in the
Bay Area"* and agents do the work in the background: search many sources and rank every
posting against your profile, with evidence quoted from your resume. What it is built toward
is **the first-day applicant**: for every posting you are eligible for, your application is
in within hours of the posting going live, filled in your own browser and submitted by you,
and the best ones get a referral attempt alongside.

This document records **what is settled and why**. The roadmap is the
[milestones](https://github.com/dcrew44/Foray/milestones) and issues on GitHub, and work
lands through pull requests.

The previous incarnation of this repo — a Python MCP-server learning prototype — is
preserved under the git tag `python-prototype`. The TypeScript server of Phase 1 (Next.js
route handlers and server actions, a Node worker on pg-boss, Drizzle, Better Auth) was
replaced in October 2026 by the Django backend this document describes; the last
pre-migration commit of `main` still runs it against the untouched `foray` database.

## Settled decisions

| Topic | Decision |
|---|---|
| Direction | **The first-day applicant** (2026-11-09). Entry-level postings are scarce and flooded with rewritten resumes within hours, so the product is judged by **posted-to-applied time** on postings the user is eligible for, not by fit score alone. The wedge is US new-grad software roles. In order: a radar that sees a posting within the hour, an application prepared before the user opens it, a fill in the user's browser with the user pressing submit, a referral attempt where the user knows someone, and a funnel that says what converted. Blind volume stays out: the user's own research found it converts at well under 1% |
| Audience | **Self-hosted open source (MIT).** Each install runs its own copy with its own accounts and API keys (once OpenRouter is a provider, one key covers every model), so nobody holds other people's resumes or mail tokens. Every domain row still carries `user_id` and every query is user-scoped, so a hosted version stays possible |
| Autonomy | **Per-run toggle**: `approve_each` (default) or `autopilot`; approvals surface in chat and in the activity feed. **Watching new postings is the exception** (#5): it deep-reviews without asking, within a daily cap the user sets (`watchDailyReviews`, default 10, about 7¢ a day) and the deep-review limit, so a strong posting is announced the cycle it arrives |
| Stack | **Django backend, Next.js frontend.** Python 3.14: Django 6.1 with django-ninja 1.7 (the JSON API, sessions, the ORM) on uvicorn, and one procrastinate 3.10 worker process. Next.js 16 only renders: server components read one Django page endpoint each, client components call Django through a typed client, and `/api/*` is rewritten to Django. Types flow from Django's OpenAPI schema into TypeScript (`pnpm api:types`) |
| LLM | **Multi-provider, no default vendor.** pydantic-ai 2.54 models (OpenAI, Google, DeepSeek, Anthropic, plus an OpenAI-compatible slot); the model for each *purpose* is an env setting, and `LLM_DEFAULT` is required at boot |
| Fetching | **Hybrid: code fetches, agents decide where.** Deterministic adapters (Greenhouse, Lever, Ashby, Workable, Recruitee, Gem, Workday, SmartRecruiters, iCIMS, JSearch) + an agent-managed company registry + an LLM extraction fallback for career pages, all through one guarded HTTP client |
| Posting mirror | **Hosted once, synced by every install.** The one hosted piece: a hub runs the board fetchers and serves what changed since a cursor, and each install applies that to its own `companies` and `jobs`. It holds public postings only. Installs keep the adapters and fetch boards themselves when the hub is down; JSearch stays per install, under that install's own key |
| Browser | Phase 1 uses headless Chromium (Playwright for Python, in the worker) only to render PDFs. **Applying runs in the user's own browser** (most likely through an extension), under the user's own identity: that is the user applying, and it passes employers' fraud checks, where a cloud browser on data-center addresses looks like fraud. No evasion (no jitter or fingerprint tricks), no CAPTCHA solving and no account creation: each of those hands off to the user |
| Applying | **One motion, and the user presses submit** (2026-11-09; it reorders the lanes below). Every eligible new posting gets a prepared application: answers planned from saved answers, one of the user's own resume variants chosen, the form filled by the extension. The deep review is no longer a gate in front of applying; eligibility (level, years, work authorization, clearance, place) is, and it is read once per posting at ingest. Unattended auto-submit comes after one-tap applying works, under the rules in the next row |
| Apply lanes | **The deep review decides.** `strong` goes to the coached lane: Foray coaches, the user writes, Foray fills the form and the user submits. `apply` goes to the automated lane: Foray applies from the user's browser. `maybe` and `skip` go to neither. A match is deep-reviewed before the automated lane takes it, because the fast score is least reliable in the band where a cutoff would fall, and the user can move any match between lanes. Every automated application waits for an approval that shows the filled form; auto-submit is an opt-in, only for applications answered entirely from saved answers, within daily and per-employer caps |
| Tailoring | **Variants the user wrote, chosen per role** (2026-11-09). The user keeps two to four resume variants, one per role family, and Foray picks one for each application: a per-posting rewrite is what the flood sends, and it costs minutes when hours matter. The tailoring pipeline and its verifier stay in the code; sending a tailored resume with an application is deferred |
| Outreach | **Warm paths before cold email**: alumni, former colleagues or a named hiring manager, with a short note the user edits and approves. Contacts come from files the user exports themself (LinkedIn's connections export, an alumni list), never from automating LinkedIn, and the alert worth most is a fresh posting at a company where the user knows someone. Gmail through Google OAuth matches employers' confirmations and replies to applications. No cold email at volume |
| Hosting | **Self-hosted on something that stays on** (2026-11-09): a small server or a home machine runs the `prod` compose profile, because a worker that runs only during a laptop session cannot be timely (the dev mirror went a week without a refresh). Applying still happens in the user's browser on their own machine. Four processes, the same in dev and prod: Postgres, Next, Django (api), the worker. Postgres runs in Docker Compose; web, api and worker run natively in dev (`pnpm dev:all`); one `Dockerfile` (a `backend` image for api, worker and migrate, a `web` image) and a `prod` compose profile run the same containers |
| Resume output | Structured resume → **PDF via HTML template**; a renderer interface can be added when a LaTeX (tectonic) renderer is compared |
| Budget | ~$100/month of third-party APIs; model calls stop for the month at `LLM_MONTHLY_BUDGET_USD` (default $90) of ledger spend |
| Workflow | Build autonomously, **a branch and a pull request for each piece of work**; the user tries it in the browser and steers |

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
| R1 | A run row and its first job commit together, or neither does, in every process. A gated start (R19) commits its run with its pending approval instead, and the first job commits with the user's yes (R6) | `runs.lifecycle.start_run` (one transaction, savepoint insert, `defer_in_tx` on Django's connection); `test_register` bans every other defer | `runs/test_lifecycle.py::test_start_is_atomic`, `runs/test_defer_in_tx.py::test_rollback_leaves_no_job_under_worker_connector` |
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
| R18 | Third-party text reaches a model wrapped: every prompt that carries a posting wraps all of it, short fields included, in `<untrusted …>` with any copy of the tag defanged inside, and the chat model reads the posting fields of tool results wrapped | `llm.untrusted`; `chat.model_view.PostingFields` | `llm/test_untrusted.py::test_the_fast_score_prompt`, `::test_the_deep_eval_prompt`, `::test_the_tailor_prompt`, `chat/test_chat_view.py::test_the_model_reads_posting_fields_wrapped` |
| R19 | The chat agent can neither start paid work nor answer an approval: the runs it asks for wait behind a `run_start` approval that only the user answers, and a no or a lapse cancels them before they run | `runs.lifecycle.start_run(gate=…)`; `runs.approvals.decide()` and `expire_lapsed()`; no chat tool answers an approval | `runs/test_start_gate.py::test_a_gated_start_waits_and_defers_nothing`, `::test_a_no_cancels_the_run_before_it_ever_ran`, `chat/test_chat_view.py::test_approvals_are_listed_in_chat_and_answered_only_on_their_cards` |
| R20 | A watch announces a match at most once, and never one a search made: it works only on the matches it created, and announces only those it reviewed itself | `matching.queries.insert_new_matches`; `matching.watch.run_watch` | `matching/test_watch.py::test_a_second_watch_reads_only_what_arrived_since_and_never_announces_twice`, `::test_a_search_s_matches_are_the_search_s` |
| R21 | A match is sent to a user's phone at most once per channel, however often a watch or a send job runs again, and never to a user who has set no topic. A send is repeated only after an attempt recorded as failed or abandoned, three attempts at most | unique `(user, match, channel)` on `notifications`; `notifications.outbox.queue_matches` (in the watch's completing transaction) and `claim` (row locks and a lease taken before any request) | `notifications/test_notify.py::test_a_watch_s_strong_match_reaches_the_phone_exactly_once`, `::test_a_second_watch_sends_only_what_is_new`, `::test_two_jobs_never_send_the_same_notification`, `::test_a_failed_send_is_retried_twice_and_then_given_up`, `::test_a_user_with_no_topic_sees_no_change` |

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
  `background` (searches and their continuations, board refreshes, the scheduled poll of the
  boards every 15 minutes) and `system` (the reaper, approval expiry, pruning, the Settings ping), so a
  long search never blocks a tailoring job the user is waiting on, and liveness never queues
  behind model work. A step is a `run_step` task: it claims the run, runs under the run's
  `llm_context` and a per-task timeout, turns a failure into `run.failed` with the user's
  sentence, and fails a run its handler forgot to finish. Only one worker works on a database
  (#51, `apps/runs/worker_lock.py`): it holds a session advisory lock, and a second one waits
  behind it, because the refresh gate and the model limiter each live in a process. That is
  what lets a worker stay up between dev sessions (`pnpm worker:up`) or on a machine that
  never sleeps (`docs/always-on.md`) without `pnpm dev:all` ever running a second beside it.
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
  stack and 15 tools, and streamed to the browser in the AI SDK's UI-message protocol, which
  `useChat` reads unchanged. Search / rank / tailor / discovery are plain procrastinate
  pipelines with approval gates, not agent loops, and Phase 2's form filling won't be one
  either: one model call plans the answers and a deterministic fill enters them.
- **Follow-ups**: `emit()` defers a `chat.follow_up` job for the moments a run reports
  (`approval.requested`, `run.completed`, `run.failed`) when the run came from a chat. The
  job waits for any streaming user turn to end, then stores an automatic update (a user-role
  message marked `run_update`, drawn as a divider) together with the agent's reply, and emits
  `chat.followup` so an open chat merges it in; the app toasts it anywhere else.
- **Approval gate pattern** (the durable-workflow substitute reused by every later phase): a
  step that reaches a gate writes an `approvals` row naming its continuation task, emits
  `approval.requested`, sets the run to `awaiting_approval`, and returns. Deciding — on its
  card, in the chat or on a page — moves the run back to `running` and defers the continuation in the same
  transaction, so a decision and its job commit together or not at all. In `autopilot` the
  same path auto-approves with `decided_via: 'autopilot'`. Unanswered approvals expire 7 days
  later through a per-minute sweep. A run the chat agent starts (a search, a deep review,
  tailoring, a company lookup) opens behind a `run_start` gate before its first step: the
  user's yes defers that step, a no or a lapse cancels the run, and autopilot never answers
  it. No chat tool answers an approval; the user answers every one on its card (R19).
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
  posting pulled from any source, shared by all users. One fetch serves everyone, and once
  the hosted mirror exists (Settled decisions), one fetch serves every install.
- **Per-user layer** (everything else): `users` itself (which holds preferences, settings —
  default autonomy, score threshold, deep-review count — interview state and time zone; there
  is no separate profile row that might be missing), then `documents`, `resumes`,
  `job_matches`, `runs`, `events`, `approvals`, `conversations`, `messages`, `notifications`
  and `llm_calls`.
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

**The radar** (#55, `apps/jobs/refresh.py`, `sources/poll.py`, `radar.py`). Every 15 minutes the
worker polls the boards that are due: a board that listed an early-career role or gave someone a
match in the last 180 days every tick, the rest hourly, careers pages every six hours
(`registry.due_companies`), and a board that fails waits twice as long after each failure. A
poll asks before it reads. It sends the validators the last answer came with (Greenhouse and
Lever honour an `ETag`; for Greenhouse the question goes to the listing without bodies, a
fiftieth of the size) and compares a digest of the body where a 304 never comes (Ashby's
`Last-Modified` is only its cache's age). An unchanged board costs one small request and
rewrites no row; a changed one is read whole and ingested as before, and every board is read
whole once a day whatever it says. Each new posting on a board checked before is timed
(`jobs.seen_lag_s`): the smaller of "since the source says it was posted" and "since the last
look, when it was not there". The Companies page reports the week's median and 90th percentile
by source, so timeliness is measured, not assumed. The empty-board guard still needs two reads:
a poll that would trip it forgets its state.

**Boards that cost a request per posting** (#57, `sources/workday.py`, `smartrecruiters.py`,
`icims.py`, `jibe.py`). Workday carries almost half of the new-graduate postings the community
lists link to, and neither it, SmartRecruiters nor a classic iCIMS portal gives a description
in its listing. Reading every posting on every poll would be thousands of requests for a board
that did not change, so these are *listed* adapters. A poll asks for the newest page and
compares its digest; when that moved, it lists the board (ids and a version each: Workday's
path and title, SmartRecruiters' release date, an iCIMS sitemap's `lastmod`), reads only the
postings that are new or whose version changed, at most 300 a pass and four at a time, and
closes the ones that left the listing. A big board fills in over several passes (`pending` in
its poll state, and no lag is recorded until it has caught up); Workday stops answering at
2,000 postings, so a listing that reaches the cap closes nothing. A Workday board is its address
(`nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite`): there is no slug to guess, so these
boards are found from links (a pasted URL, a careers page, an aggregator's apply link), never
by trying names. iCIMS is two products. Its newer career sites (the Jibe API at
`/api/jobs`) return whole postings a page at a time and ask for five seconds between requests;
its classic portals publish a sitemap and JSON-LD on each posting page, and a portal that
redirects to a career site is registered as that site. SmartRecruiters' Posting API is
documented as public, but `api.smartrecruiters.com/robots.txt` disallows generic crawlers, so
Foray reads a SmartRecruiters board only for a company someone added and never discovers them
in bulk (#69). A pass reads at most 1,200 postings of one source, four at a time per host and
sixteen in all, so a hundred Workday boards registered together fill in over a day of passes
without holding up the fifteen-minute poll of everything else.

**Where the list of boards comes from** (#58, #28, `apps/jobs/candidates.py`,
`crawl_index.py`). The radar is only as wide as the registry, and the registry began as
somebody else's list under a non-commercial licence. A board id is a fact that shows in a URL,
so Foray collects its own from Common Crawl: `find_boards` bisects a crawl's `cluster.idx` by
byte range for each board host and reads only the index blocks under it (a few seconds and a
few megabytes a host, against the crawl's static files; the query server in front of them is
slow and rate-limited). Each id becomes a `board_candidates` row with the crawls it was seen
in. `grow_registry` then asks each candidate's board once, newest-seen first, and registers
the ones that answer with postings, a software role among them: a third of the boards in a
crawl belong to clinics, schools and shops, and every board followed is polled for good. The
answer that proved a board real is the one ingested, with the poll state it left, so nothing
is fetched twice. A Workday site must also answer its own search (`--matching "software
engineer"`), because most of the seven thousand in a crawl are hospitals and retailers. Every
company records each place it was learned from (`companies.found_in`), a company that was in
the old seed and is also in a crawl is no longer only the seed's, and the `seed.json` that
ships is rewritten from boards seen somewhere public and checked live. Community new-graduate
lists without a licence are a yardstick for coverage and never an import; one under MIT
(`ambicuity/New-Grad-Jobs`) is a second source, credited in the README. On 2026-11-09 this took
the registry from 404 active companies to 3,236 (39,351 open postings to 155,593, and 818
early-career software roles to 2,038, before the 207 Workday sites among them had been read),
and the shipped list from 406 entries under somebody else's licence to 3,222 of Foray's own.
Fourteen boards of the old list appear in no crawl; they stay in databases that already had
them, marked `found_in = ["seed"]`, and are not in the list that ships.

**What a posting asks** (#56, `apps/jobs/facts.py`). Whether someone can get a job at all turns
on a few facts that are the same for every reader: the role's level, the years it requires, what
it says about work authorization, whether it needs a clearance. They are read once, at ingest,
by patterns over the title and the cleaned description, into columns on `jobs` (`level`,
`years_min`, `years_preferred`, `work_auth`, `clearance`, with the sentence each came from in
`facts`), and read again only when the posting changes or `facts.VERSION` does. The reading
errs toward saying nothing: a posting wrongly called senior would vanish from a junior's search,
while one called `unknown` only costs a fraction of a cent to score. A junior seeker's prefilter
then drops senior and staff roles and postings that require more years than the level allows
(`prefilter.YEARS_LIMIT`: an intern 1, a new graduate 2, entry level 3); nobody else is filtered.
No model is involved. Against 210 postings labelled by hand (`tests/fixtures/facts/`,
`manage.py facts_benchmark`) the patterns agree on level and years for 97 and 100 of the 100
they were tuned on, and for 59 and 57 of 60 labelled afterwards and measured once (60 and 59
since three of the misses it showed were fixed, which makes that set a regression check rather
than a test), so the model the issue allowed for was never needed. The match page shows the facts with their sentences.

**Whether a posting is worth an application** (#7, `apps/jobs/legitimacy.py`). Four signs that
it may not be, each a rule over what the mirror already holds, with no model and no fetch. A
posting is `closed` when it is no longer on the employer's board (`is_active` false; the page
says when it was last seen). It is `reposted` when at least three postings with distinct
external ids share its ingest dedupe key (company, normalised title, primary location) and its
source, and were first seen in the last 180 days, closed ones included. Only its own source is
counted because that key exists to make an aggregator's copy collapse with the board's own, and
a copy is not a repost. It is `long_open` when still open more than 60 days after it was posted,
or first seen when the source gives no date. And an aggregator's posting has an
`aggregator_link` when its apply link's host is neither the employer's nor a job board Foray
knows. The employer's is the registered company's domain, else the website the aggregator's
own payload states, or any domain named exactly for the employer; the boards are the ones
discovery recognises, matched against the link's host so that a board's address in some other
site's path or query does not pass. Unlike the facts, nothing is stored: a signal is true of
today, so the match page works them out when it is read, in one query (the repost count, the
company's domain and the database's present together), and shows them under "Before you
apply". Only a closed posting is prominent, since it alone decides whether applying is
possible. The signals inform and do not filter: the search pipeline never reads them (it
already leaves closed postings out by `is_active`).

**Watching new postings** (#5, `apps/matching/watch.py`). Between searches, strong roles used
to sit unscored in the mirror for a week. A scheduled refresh that brought postings in (and
every sixth hour's tick, for what the review cap held back) defers
`watch.fan_out`, which starts a `watch` run for each user who is watching (resume, complete
preferences, no search queued or running, budget left). A watch is the pipeline above with no
request, no refresh and no gates, over the postings first seen since the previous completed
watch (less ten minutes; never more than a week back). It works only on the matches it creates
plus those earlier watches of the week left unfinished, so a search's matches stay the search's
and nothing is announced twice (R20). Deep reviews run within the daily cap, counted from the
ledger over 24 hours. A watch that reviewed something `strong` or `apply` completes linked to the
user's "New matches" chat, so the usual follow-up posts there, and Home lists such matches on
postings first seen in the last week under "New this week" (by `first_seen_at`, not when the
match was made, so a search's month-old postings stay out); a quiet watch completes at debug level and stays out of the
feeds. Announced matches stay in `found`: the user shortlists. The worker must be running for any
of this; in dev that means `pnpm dev:all`, whose worker runs the missed tick when it starts.

**To a phone** (#59, `apps/notifications/`). The chat is read only with Foray open, and a
posting found at 2 p.m. is no use at 9 p.m., so what a watch announces is also published to the
user's [ntfy](https://ntfy.sh) topic: the company and title, the verdict, how long the posting
has been up, and a link to the match. ntfy needs no account with Foray and no app of ours: the
user subscribes to a topic name only they know and pastes it into Settings. The server is the
install's (`NTFY_SERVER`, the public ntfy.sh by default) and never a user's value, so the one
guarded HTTP client's rules hold; a self-hosted ntfy therefore has to resolve to a public
address for now. `finish_watch` queues one `notifications` row per announced match in the
transaction that completes the watch (`outbox.queue_matches`, before `emit()`), and nothing at
all for a user with no topic. The `notify.send` job deferred with it, and a sweep of the same
task every minute, send what is due. `(user, match, channel)` is unique, and the sender leases
the rows it claims (it moves `send_after` ahead and counts the attempt before any request goes
out), so a second watch, a job delivered twice or two jobs at once never send a match twice
(R21). Everything else is decided when a row is due, from the settings as they are then: quiet
hours on the user's clock (`users.timezone`, which chat and Settings keep equal to the
browser's; 22:00 to 08:00 unless changed) move `send_after` to their end, a topic since removed or a match the user has already moved out of `found` makes the
row `skipped`, and a send that fails is tried twice more (after one minute and after ten) before
the row is `failed`. More than five due at once go out as the five best and one line for the
rest. One attempt is one request with no retry inside the client: the only way to a duplicate
left is an answer lost after ntfy had accepted the message. Settings' "Send a test
notification" is a job as well (`notify.test`, answered with a `notification.test` event, like
the worker's own test), so no request waits on ntfy (invariant 12) and a test that arrives has
taken the path a real notification takes.

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
| Board adapters | `greenhouse`, `lever`, `ashby`, `workable`, `recruitee`, `gem` and `jibe` (iCIMS career sites) share one protocol: `fetch_board(client, slug) -> list[NormalizedJob]`, raising `BoardNotFound` on a 404, in two halves (`board_url(slug)`, `parse_board(slug, body)`) so a poll can ask before it reads, plus an optional cheaper `probe_url(slug)` | more boards |
| Listed adapters | `workday`, `smartrecruiters` and `icims` (classic portals) list a board without its descriptions and read a posting at a time: `count`, `probe` (a digest of the newest page), `list_board -> Listing` (each posting's id and a version) and `read_posting`. `sources.LISTED_ADAPTERS`; `refresh._refresh_listed` reads only what is new or changed | Oracle, Taleo, SuccessFactors |
| Other sources | `jsearch` and `careers_page` are plain functions with the same `NormalizedJob` output | — |
| `Storage` | `LocalStorage` (`apps/core/storage.py`: `put`, `open`, `delete`, `exists`) | S3 / Blob behind the same four methods |
| `ResumeRenderer` | future work: today there is one renderer, the HTML template in headless Chromium | `latex` via tectonic, compared side by side |
| `BrowserProvider` | future work (Phase 2) | the user's own browser, most likely through an extension; never a cloud browser |

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

The milestones on GitHub hold the order and the work; this section keeps what each phase is
and why.

**Order since 2026-11-09** (Direction, under Settled decisions). The phases below were
written before it and keep their designs; the order of work is now:

1. **Radar.** An always-on worker, boards polled every 15 to 30 minutes with the
   posted-to-seen lag reported, Workday, iCIMS and SmartRecruiters coverage, a company list
   in the thousands, and each posting's level, years, work authorization and clearance read
   once at ingest.
2. **Apply in one motion.** Phase 2's fill, saved answers and proof of arrival, started from
   an alert: the watch prepares the application and the user opens it filled.
3. **Referrals.** Phase 3's warm paths, fed by contacts the user imports.
4. **Outcome loop.** Gmail, read-only, matches confirmations, rejections and interviews to
   applications; the funnel reports interview rate by posted-to-applied time, source, resume
   variant and referral. Whether applying early helps is a claim this measures, not one the
   design assumes.

The career coach and model choice milestones come after these. What decided it, from the
development database that day: the mirror had not refreshed for a week, 528 matches had
become 20 shortlisted and 1 applied, and the 406 companies were nearly all on Greenhouse,
Ashby and Lever.

- **Phase 1 — find, rank, tailor** (no submitting). **Complete**: onboarding (resume
  extraction, preferences interview or form), search across ~200 boards + JSearch,
  fast scoring, deep review and shortlisting with approval gates, the pipeline board,
  tailored resumes with a fact-checker, PDF rendering, usage and model settings. Rebuilt on
  the Django backend in October 2026 with the same behaviour, plus run cards for every slow
  action, a monthly spend guard and per-user throttles.
- **Phase 2 — apply, in two lanes.** Both lanes fill forms in the user's own browser, and
  the rule between them is under Settled decisions. The design comes from a read-only spike
  on Greenhouse, Lever and Ashby forms:
  - **Per-ATS schema extraction.** Greenhouse's job API with `?questions=true` gives the
    exact field names and option labels; Lever's `/apply` is a plain HTML form; Ashby is
    React with no `<form>`, so its fields are read from the page.
  - **One model call plans the answers, then a deterministic fill enters them.**
    Sponsorship, salary and years of experience come only from answers the user confirmed
    (`application_answers`), never from generation, and EEO questions are declined unless
    the user saved an answer.
  - **An `apply_submit` approval** shows a screenshot of the filled form, the answers and
    the resume it sends. Approving re-opens the form and re-fills it from the approved
    answers, so no browser waits open on a decision.
  - **Hand-offs.** Greenhouse and Ashby use reCAPTCHA and Lever hCaptcha, so many
    submissions end with a filled form the user submits; a login or an account to create
    hands off the same way.
  - **Proof of arrival** on each application: the confirmation page, the exact PDF and
    answers sent, and the time.
  - **Restraint in the backend:** per-employer and daily caps on the automated lane.

  Built so far: **saved answers** (#21, `apps/applications/answers.py`). The questions forms
  share are a catalogue (work authorization, sponsorship, citizenship, clearance, pay, start
  date, links, the voluntary disclosures) that the Profile page asks once. A form field is
  classified by its label and answered only from what the user saved: an unanswered question
  is `missing` and waits for them, an unanswered voluntary disclosure is declined, and a
  question of the employer's own is kept under its words so the next employer who asks it is
  answered too. The same answers decide eligibility: someone who said they need sponsorship
  is not shown postings that say they will not sponsor or are for citizens only, and someone
  who said they hold no clearance is not shown postings that require one. Only an answer the
  user gave rules anything out.

  **Prepared applications** (#60, #62, `apps/applications/prepare.py`). When a watch keeps a
  match (its fast score clears the user's threshold) it starts that match's application at
  once, beside the deep reviews and without waiting for them; a match page can start one too.
  The worker reads the posting's form from its board (Greenhouse publishes every question,
  its options and whether it is required, with the posting; for other boards only the fields
  every form has are planned, and the browser extension reads the rest when it fills) and
  plans each field: name, email, city and links from the newest base resume, the resume file,
  the user's saved answers mapped onto the form's own options, a declined voluntary
  disclosure, or `missing`. One required field with nothing to put in it makes the
  application `needs_answers`, with the question named; otherwise it is `ready`. The Apply
  page lists what is ready, the posting that has been up longest first, then what waits on an
  answer with the question inline: answering it saves the answer under the question's own
  words (and under the shared question when it says what that asks), and every open
  application is planned again without another fetch. Each application keeps when it was
  prepared, became ready, was opened and was sent, which is what posted-to-applied time is
  measured from. No model is involved, and the only request is the GET that reads the form.

  It adds the `applications` and `application_answers` tables, the `apply` and
  `career_page_scan` run kinds and the `apply_submit` approval. Tests use local replicas of
  the forms; a real application is submitted only on the user's explicit yes.
- **Phase 3 — warm paths.** For each shortlisted company, look for a warm path first
  (alumni, former colleagues, a named hiring manager) and draft a short note the user edits
  and approves. Gmail through Google OAuth (an `oauth_accounts` table with encrypted offline
  tokens, ID tokens verified against Google's JWKS; the "Continue with Google" sign-in
  returns with it) matches employers' confirmation emails to applications, and later moves
  stages on rejections and interview invitations.
- **Phase 4 — ready for other people.** One-command setup with the user's own keys
  (OpenRouter as a provider makes one key enough), the hosted posting mirror that installs
  sync from, a company list built from Foray's own discovery in place of the seed list
  (done in November 2026: #58, #28),
  per-user company follows instead of one shared status, and a retention policy. No hosted
  app, no usage billing and no cloud browser.

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
- **A match leads with its verdict.** Every row in the Matches list and board, and the top
  of the match page, says three things: the review's recommendation (or "Not reviewed"),
  one line of why (the first sentence of the review's summary, else the fast score's first
  reason), and a blocker when there is one (the first disqualifying gap, else the first
  missed hard requirement). The match page then says how many of the posting's
  requirements the resume meets, before the evidence for each. One vocabulary names the
  fit: the recommendation labels, with the pine scale's steps on the rubric bands. A
  reviewed score is marked in pine at every size, never in the signal colour.

LinkedIn automation through the user's own account is off the roadmap: LinkedIn's terms
forbid it, and applying to a LinkedIn listing stays a hand-off. The career-counselor idea
became the career coach (coaching per match, and gaps that recur across top matches); it
adds its own table or column when it is built, and nothing is reserved for it.

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
| Prompt injection through postings | every prompt that carries posting text (scoring, deep review, tailoring, careers pages, follow-up notices) wraps all of it, short fields included, in `<untrusted source=…>`, defangs any copy of the tag inside, and says the block is data (`apps/llm/untrusted.py`); the chat model reads the posting fields of tool results wrapped too, while the cards keep the plain values (`apps/chat/model_view.py`); follow-ups and a re-sent notice get read-only tools; the chat agent cannot answer an approval, and the work that spends (a search, a deep review, tailoring, a company lookup) waits behind a `run_start` approval the user answers on its card (R19). Cheaper writes (preferences, a pipeline stage, adding one company, stopping a run) still act on the model's say-so |
| API drift vs. model training data | `AGENTS.md` and the vendored AI SDK skills for the frontend; pydantic-ai pinned exactly (it ships several times a week), Django and ninja pinned; golden and contract tests pin prompts, wire bodies and the UI-stream protocol |
| Tailoring fabrication | fact-grounded prompt + verifier with evidence ids; the user sees every change in the tailor panel, and once Phase 2 lands a tailored resume goes out only with an automated application, whose `apply_submit` approval shows it |
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
