# CLAUDE.md

Working notes for Foray. Read [DESIGN.md](DESIGN.md) for *why*; this file is *how*.

Foray is a Django backend (`backend/`, Python 3.14 through uv) and a Next.js 16 frontend
(`src/`, Node 24 through pnpm). Next only renders; every `/api/*` request is rewritten to
Django. Four processes run in dev and prod alike: Postgres, web (:3000), api (:8000) and the
worker.

## Commands

| Command | What it does |
|---|---|
| `pnpm dev:all` | web, api and worker together (`foray-web` in `.claude/launch.json`); the browser only talks to :3000 |
| `pnpm dev` / `dev:api` / `dev:worker` | one of them: `next dev`; uvicorn with `--reload` on :8000; `manage.py worker` under watchfiles, which restarts it on every `.py` save |
| `pnpm build` | production build of the frontend; run it before committing UI or route changes |
| `pnpm db:up` / `db:down` | Postgres 18 in Docker (`docker compose up -d db`, container `foray-db-1`) / stop it |
| `pnpm db:create` | create the `foray_app` database (once per Postgres volume) |
| `pnpm db:migrate` / `db:makemigrations` | Django migrations |
| `pnpm dev:session [email]` | print `cookie: sessionid=<key>` for a local user, else the oldest one (DEBUG only) |
| `pnpm auth:set-password <email>` | set a forgotten password at a hidden prompt (no mail transport yet); signs out every session |
| `pnpm seed:companies [--fetch]` | load `backend/apps/jobs/seed.json` into `companies`; `--fetch` also fills the mirror |
| `pnpm fetch:board` / `verify:boards` | print what an adapter makes of one board / check that board slugs are real (neither touches the database) |
| `pnpm test` | `test:web` (Vitest, `src/test/unit/format.test.ts` only), then `test:api` (pytest) |
| `pnpm lint` / `typecheck` | ESLint, `ruff check` and `ruff format --check` / `tsc --noEmit` and pyright |
| `pnpm api:types` | export `backend/openapi.json` and regenerate `src/lib/api/schema.d.ts` from it |
| `pnpm check` | lint, typecheck, test, `api:types`, then fails if the generated files changed; run it before committing |
| `pnpm llm:smoke [purpose …]` | one call per configured purpose, to check keys and pricing (`LLM_FAKE=1` for $0) |
| `pnpm llm:bakeoff` | compare models on fixed fixtures (cost, latency, schema failures, agreement) |
| `pnpm db:backup-legacy` / `db:import-legacy` / `db:verify-import` | the one-off, read-only import of the old TypeScript app's `foray` database into `foray_app` |
| `pnpm docker:prod` | `docker compose --profile prod up --build`; it wants :3000 too, so stop `dev:all` first |

Backend-only commands run as `cd backend && uv run python manage.py <command>`:

| Command | What it does |
|---|---|
| `create_test_user [--email e] [--with-resume] [--with-match]` | a throwaway `test-…@example.test` user with an unusable password (DEBUG only); `--with-resume` adds a sample resume and complete preferences, `--with-match` one match on the newest job |
| `create_test_user --purge` | delete every `@example.test` user with their rows, sessions and files |
| `worker` | the worker itself (what `dev:worker` runs, without the reload) |
| `acceptance_checks [--since ISO]` | re-check the database-level invariants after a rehearsal; exits 1 on any FAIL |
| `debug_sleep <seconds>` | a job that sleeps, for checking lock recovery after a `kill -9` (DEBUG only) |

## Layout

- `backend/foray/`: the Django project. `settings.py`, `settings_test.py`, `env.py` (every
  variable validated at boot; a bad one names itself and exits 1), `api.py` (the one
  `NinjaAPI`), `urls.py`, `asgi.py`, `middleware.py` (Origin check, sliding session, stream
  headers).
- `backend/apps/<app>/`: `core` (the `db()` bridge, `defer_in_tx`, scoping, errors, the router
  factory, the HTTP client, storage, throttles), `accounts`, `llm`, `jobs` (with
  `sources/<source>.py`), `matching`, `resumes`, `runs`, `chat`, `pages`, `legacy`. An app
  keeps its models and services, and as it needs them `api.py` (its routers), `tasks.py` (its
  procrastinate tasks), `fakes.py` (its `LLM_FAKE` answers) and `management/commands/`.
- `backend/tests/`: pytest, one folder per app, plus `fixtures/`, `goldens/` and `contract/`.
- `src/app/`: pages only (no route handlers, no server actions). `src/components/`: UI.
  `src/lib/api/`: `server.ts` (`django()`, `getMe()` for server components), `client.ts`
  (`api` and `call()` for client components), `types.ts` and `chat-types.ts` (friendly names
  over the generated `schema.d.ts`), `auth.ts`.

## Boundaries

- **The frontend imports no server module.** Next reaches the backend only over HTTP: a page
  is one `django("/api/pages/…")` read plus JSX; a client component calls
  `call(api.POST(…))` and then `router.refresh()`. A `"use client"` module must not import
  `src/lib/api/server.ts`, even for a type (ESLint `no-restricted-syntax`).
- **Python layering.** Services are plain sync functions over the ORM. Async code (the chat
  view, tasks, fetchers, model calls) reaches them only through `await db(fn, …)`; it never
  touches the ORM itself, and sync code never awaits. So a transaction never spans a model
  call or an HTTP fetch.
- `tests/test_register.py` enforces what a type checker cannot: `sync_to_async` only in
  `core/db.py` and `chat/views.py`; `httpx2` only in `core/http.py`; `_plain` and
  `_default_manager` only in scoping, the legacy import and the reaper; no `get_object_or_404`
  on a per-user model; chat tools are `async def`; no `.defer(`, `.defer_async(` or
  `defer_job` outside `core/queue.py`; no `Router(` outside `core/router.py`; no `auto_now`.

## Conventions

- **Python 3.14 through uv** (`backend/pyproject.toml`; pydantic-ai is pinned exactly).
  **Node 24+, ESM only, pnpm**, TypeScript pinned to `~5.9`.
- **`await db(fn, *args)`** is the one bridge from async code to the database: it runs `fn`
  on a pooled thread and hands the connection back. It refuses coroutine functions.
- **Per-user rows go through `for_user`**: `JobMatch.objects.for_user(user_id)`,
  `create_for(user_id, …)`, `bulk_create_for`. System code (reaper, cron, ledger totals, the
  import) says why: `unscoped("reaper")`. Plain `Model.objects.filter(…)` raises.
- **Ask for an LLM purpose, never a model**: `model("score")`, or
  `structured("score", FastScore, instructions=…, prompt=…)`. Vendor quirks (reasoning
  knobs, unsupported `temperature`, DeepSeek's thinking switch) live only in
  `apps/llm/reasoning.py`. The ledger is a wrapper on the model (`apps/llm/ledger.py`), so
  every request is recorded; attribute it with `llm_context(user_id=…, run_id=…,
  conversation_id=…)`, which `run_step` and the chat view already set.
- **Heavy work is a run**, never inline in a request: `lifecycle.start_run(user_id, kind,
  input=…, dedupe_key=…, task=…)` inserts the run and defers its first job in one
  transaction, and the endpoint answers `202`. Every defer goes through `defer_in_tx` (or `defer_step` for
  a run's later steps); never call `.defer()`.
- **Steps are `run_step` tasks** (`apps/runs/step.py`): `run_step(name, queue=lane,
  timeout_s=…)` claims the run, sets the ledger context, applies the timeout and turns an
  exception into `run.failed`. A new task needs a row in `step.TASKS` (its lane and timeout),
  and `retry=0` if it can call a model.
- **Runs end only through `lifecycle.complete()`, `fail()` or `cancel()`**, each conditional
  on an active run, so every kind ends with exactly one `run.completed`, `run.failed` or
  `run.cancelled`. A new run kind goes into `apps/runs/catalogue.py` (kind, title, failure
  prefix, input and output models).
- **`emit()` is last.** Progress goes through `runs.events.emit()`, the only writer of
  `events`. It takes a per-user advisory lock, so it must be the last statement of its
  transaction, after every row lock (only new-row inserts such as defers may follow).
  Long-running steps emit progress so the activity feed moves; check `is_cancelled` between
  stages and write nothing once a run has stopped.
- Approval gates use the continuation pattern described in DESIGN.md: a step that needs a
  decision calls `approvals.request_approval(...)` and returns; `decide()` defers the
  continuation in the same transaction.
- **A search follows its request.** The search pipeline reads the effective preferences,
  `apply_brief(saved, brief)` (`apps/matching/brief.py`), never the saved preferences
  directly: a request for entry-level roles at one company overrides level, roles, places,
  remote and companies for that run only.
- **Runs started from chat report back there.** `emit()` defers a `chat.follow_up` job for
  `FOLLOW_UP_KINDS` (`apps/runs/catalogue.py`, mirrored in `src/lib/chat/follow-up-kinds.ts`;
  a test keeps them equal) when the run has a conversation. Follow-up turns get read-only
  tools only (`FOLLOW_UP_TOOLS` in `apps/chat/tools.py`).
- **The API**: routers come from `apps.core.router.router()` (camelCase on the wire, snake_case
  in Python); output schemas extend `ApiModel`. Mutations answer `{ok: true, message}` (`202`
  when work was queued); expected failures raise `ApiError(status, message)` or name their
  sentences with `@messages(invalid=…, not_found=…)`. Users never see raw pydantic text. A
  model the frontend needs outside an operation is added with `register_schema`. After any
  change to a response shape, run `pnpm api:types` and commit both generated files.
- **Pages**: each Next page reads one `/api/pages/<page>` endpoint (`apps/pages/build.py`),
  and each of those has a query budget in its tests.
- **Models**: Django migrations own the schema. No `auto_now` (set timestamps explicitly);
  `UniqueConstraint(fields=[...])`, never positional strings; enums are `TextChoices` with a
  `choices_check` CHECK. Make a new migration with `pnpm db:makemigrations`.
- **Outbound HTTP** goes through `apps.core.http.HttpClient` only: it refuses private and
  metadata addresses on every hop and caps response sizes.
- **Job ids** are `source:company_slug:external_id`; career pages use
  `web:<slug>:<sha1(url)>`.
- **Never rewrite `description_raw`.** Cleaning produces `description_text`.
- **Configuration** lives in the repo-root `.env`, shared by Next and Django, and is declared
  in `backend/foray/env.py` (and documented in `.env.example`). Read settings through
  `settings.ENV`, never `os.environ`.

## UI conventions

- Dark is the default theme and the one most people see; check new UI in dark first,
  then light. Theme logic lives only in `src/lib/theme.ts`.
- Tokens live in `src/app/globals.css`; never hardcode colors. `signal` (marigold) means
  only "agents working" or "waiting on the user"; `destructive` only errors; match fit uses
  the `fit-0..5` pine scale via `ScoreChip`.
- Build pages from `src/components/foray/primitives.tsx` (`PageBody`, `PageHeader`,
  `Section`, `EmptyState`, `ScoreChip`, `CompanyMark`, …) and format with `src/lib/format.ts`.
- Sentence case; no all-caps labels, no "A · B · C" meta strings, no "→" in buttons.
- Live state comes from `useLive()` (one shared poll of `/api/live`). Never add a
  per-component poll; call `requestLiveRefresh()` after starting work, and use
  `<RefreshOn kinds={[...]}/>` to re-render a server page when an event lands.
- A component that starts a run and reports its outcome itself registers it with
  `watchRuns([runId])` and toasts with `{ id: runToastId(runId) }`, so each outcome toasts
  once.
- Types come from the generated schema (`@/lib/api/types`, `@/lib/api/schema`), never
  hand-written copies of a Django response.
- shadcn components here are the Base UI flavour: use the `render` prop, not `asChild`.

## Driving the app in a browser

Verify UI in the built-in browser pane (`preview_start` with `foray-web`, which runs
`pnpm dev:all`) so the user can watch; headless Playwright scripts are for extra screenshots,
not the only check.

- Never type a password. Make a throwaway user and mint a session for it:
  `cd backend && uv run python manage.py create_test_user --with-resume` prints
  `user: test-…@example.test`, and `pnpm dev:session test-…@example.test` prints
  `cookie: sessionid=<key>`. Drop the `cookie: ` prefix and set the rest with
  `document.cookie` in the pane. (`pnpm dev:session you@example.com` does the same for an
  existing local user.) Do not revoke a session the pane is still using, and purge the
  throwaway users afterwards (`create_test_user --purge`).
- The pane's first request slides a dev session into an `HttpOnly` cookie, which
  `document.cookie` can no longer replace. To switch users, sign out (Settings or the account
  menu), or let one request with a dead session (after a purge) get its 401, which clears the
  cookie; then set the new one.
- Typing and clicking need the pane on screen, and a click by a stale `ref` can miss:
  focus the composer with JavaScript and check its value before pressing Enter.
  While the pane is hidden, screenshots show an old frame and the app polls every
  6 to 30 seconds, so read state with `get_page_text` instead, and
  `window.dispatchEvent(new Event("foray:live-refresh"))` to poll now.
- Don't edit files while typing into the pane: Fast Refresh can reset the composer.
- The watch-mode worker restarts on every `.py` save and interrupts whatever it was running;
  the reaper fails such runs ("Interrupted…") within about two minutes, so re-run after edits
  settle. A worker killed with `kill -9` is not restarted until the next save.
- `LLM_FAKE=1` (with `DEBUG=true`) runs every model as a deterministic fake at $0;
  `LLM_FAKE_DELAY_MS` slows it enough to press Stop, and `LLM_FAKE_FAIL=<purpose>` makes one
  fail. Set them in the process environment for a rehearsal rather than editing `.env`.

## Tests

- **pytest never touches the dev database.** It builds `test_foray_app` from the migrations
  and keeps it between runs (`--reuse-db`; pass `--create-db` after changing migrations, and
  set `FORAY_TEST_DB_NAME` to run two suites side by side). `settings_test.py` ignores `.env`
  and the shell, and a fixture blocks every non-loopback connection, so no test reads your
  keys, reaches a provider or spends.
- Fakes and helpers: `tests/factories.py` (`make_user`, `make_job`, `make_match`, …),
  `tests/fakes.py` (`fake_models(monkeypatch, {purpose: FunctionModel(...)})`, which keeps
  the budget, limiter and ledger in the path), and `tests/queue.py` (`deferred()` to assert
  jobs, `run_deferred()` to run them with a real in-process worker). Tests that use
  `run_deferred()` or `db()` threads are `django_db(transaction=True)`.
- Goldens in `tests/goldens/` pin prompts, adapter output, templates and the UI-stream
  protocol byte for byte; `tests/contract/` checks streams with the installed `ai` package
  (it needs `node` and `pnpm install`). Tests marked `slow` render PDFs with a local
  Chromium or Chrome; `-m "not slow"` skips them.
- `FORAY_REGISTER_STRICT=1` makes `tests/test_register.py` fail when a test that DESIGN.md's
  invariant register names does not exist. Run the full suite with it before committing.
- Vitest covers only the frontend's pure helpers (`src/test/unit/format.test.ts`).

## API drift warning

pydantic-ai 2.54, django-ninja 1.7, Django 6.1, Next 16 and AI SDK 7 all differ from model
training data. Read the installed source (`backend/.venv/lib/python3.14/site-packages/…`,
`node_modules/…`) before relying on an API you remember. Notably:

- **pydantic-ai 2.54**: `Agent(model, instructions=…, output_type=…, deps_type=…)`;
  `run_stream_events(...)` is an async context manager, not an iterator; models compose as
  `WrapperModel` subclasses; the unified `thinking` setting is silently dropped for DeepSeek
  (which is why `reasoning.py` sends its own switch); the UI stream comes from
  `pydantic_ai.ui.vercel_ai` (`VercelAIEventStream`, subclassed in `apps/chat/uistream.py`).
- **django-ninja 1.7**: an operation dumps with an explicit `by_alias=False` unless its
  router was built with `by_alias=True`, so always use `core.router.router()`; answer `202`
  with `ninja.Status(202, body)` (a tuple logs a deprecation warning); component names are
  global in `openapi.json` and one name may have only one schema (`tests/core/test_openapi_names.py`).
- **Django 6.1**: positional arguments to `UniqueConstraint` are expressions, so use
  `fields=[...]`; `CheckConstraint` takes `condition=`; there are no async transactions.
- **Next 16**: `proxy.ts` replaces `middleware.ts`; heed `AGENTS.md`, where Next re-writes its
  block and points at `node_modules/next/dist/docs/`.
- **AI SDK 7** (frontend only: `useChat`, `DefaultChatTransport`, `UIMessage`): read the
  vendored skills in `.agents/skills/` (`ai-sdk` and `migrate-ai-sdk-v6-to-v7`, symlinked into
  `.claude/skills/`).

## Workflow

Build autonomously; commit each working step to `main` with the step's "Done" check
actually verified. Stop and ask only for things the user must do (API keys, starting
Docker Desktop, creating accounts) or at a genuine fork not covered by the plan.
