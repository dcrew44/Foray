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
| `pnpm dev` / `dev:api` / `dev:worker` | one of them: `next dev` on 127.0.0.1 (never the LAN); uvicorn with `--reload` on 127.0.0.1:8000; `manage.py worker` under watchfiles, which restarts it on every `.py` save |
| `pnpm build` | production build of the frontend; run it before committing UI or route changes |
| `pnpm db:up` / `db:down` | Postgres 18 in Docker (`docker compose up -d db`, container `foray-db-1`) / stop it |
| `pnpm db:create` | create the `foray_app` database (once per Postgres volume) |
| `pnpm db:migrate` / `db:makemigrations` | Django migrations |
| `pnpm dev:session [email]` | print `cookie: sessionid=<key>` for a local user, else the oldest one (DEBUG only) |
| `pnpm rehearsal` | `dev:all` against a scratch database (`foray_rehearsal`, created, migrated and seeded with nine real boards on first use), its own storage folder and `LLM_FAKE=1`; `foray-rehearsal` in `.claude/launch.json`. Stop `dev:all` first: it uses the same ports |
| `pnpm rehearsal manage <args>` / `pnpm rehearsal -- <command>` | `manage.py <args>`, or any command, in the rehearsal environment (`pnpm rehearsal -- pnpm dev:session`) |
| `pnpm rehearsal:reset` | drop the scratch database and its files |
| `pnpm auth:set-password <email>` | set a forgotten password at a hidden prompt (no mail transport yet); signs out every session |
| `pnpm seed:companies [--fetch]` | load `backend/apps/jobs/seed.json` into `companies`; `--fetch` also fills the mirror |
| `pnpm fetch:board` / `verify:boards` | print what an adapter makes of one board / check that board slugs are real (neither touches the database) |
| `pnpm test` | `test:web` (Vitest, the pure helpers in `src/test/unit/` and `extension/test/unit/`), then `test:api` (pytest) |
| `pnpm lint` / `typecheck` | ESLint (the app, and `extension/` under its own rules), `ruff check` and `ruff format --check` / `tsc --noEmit` and pyright |
| `pnpm api:types` | export `backend/openapi.json` and regenerate `src/lib/api/schema.d.ts` from it |
| `pnpm check` | lint, typecheck, test, `api:types`, then fails if the generated files changed; run it before committing |
| `pnpm llm:smoke [purpose …]` | one call per configured purpose, to check keys and pricing (`LLM_FAKE=1` for $0) |
| `pnpm llm:bakeoff` | `--purpose p --models a:b@effort,… --reference c:d [--out f.json]`: compare models for one purpose on fixed fixtures (`score` and `evaluate` so far) by cost, latency, schema failures and agreement with the reference; real calls, ledgered as `bakeoff` |
| `pnpm db:backup-legacy` / `db:import-legacy` / `db:verify-import` / `db:mark-cutover` | the one-off, read-only import of the old TypeScript app's `foray` database into `foray_app`; marking the cutover makes `--replace` and `reset_app_db` refuse |
| `pnpm docker:prod` | `docker compose --profile prod up --build`; it wants :3000 too, so stop `dev:all` first |
| `pnpm worker:up` / `worker:down` / `worker:logs` | a worker container that stays up between dev sessions, on the everyday database and storage; built from the checkout, so start it from `main` |
| `pnpm backup` | dump the database and archive the stored files into `backups/`, keeping the newest 14 |

Backend-only commands run as `cd backend && uv run python manage.py <command>`:

| Command | What it does |
|---|---|
| `create_test_user [--email e] [--with-resume] [--with-match] [--with-contact]` | a throwaway `test-…@example.test` user with an unusable password (DEBUG only); `--with-resume` adds a sample resume and complete preferences, `--with-match` one match on the newest job, `--with-contact` that match and a made-up contact at its company |
| `create_test_user --purge` | delete every `@example.test` user with their rows, sessions and files |
| `worker` | the worker itself (what `dev:worker` runs, without the reload) |
| `acceptance_checks [--since ISO]` | re-check the database-level invariants after a rehearsal; exits 1 on any FAIL |
| `debug_sleep <seconds>` | a job that sleeps, for checking lock recovery after a `kill -9` (DEBUG only) |
| `read_facts [--all]` | read level, years, work authorization and clearance for postings that lack them (after the migration that adds the columns, or after `facts.VERSION` changed); `--all` reads every posting again |
| `facts_benchmark [--show]` | how often the fact reader agrees with the 210 hand labels in `tests/fixtures/facts/`; the posting texts come from `notes/benchmarks/posting-facts/` or the mirror; exits 1 under 90% on level or years |
| `find_boards --latest 2` / `--crawl CC-MAIN-…` / `--file f.tsv` / `--urls any.yml --found-in list:owner/repo` | collect board ids as registry candidates, from Common Crawl's index, a TSV (`source<TAB>id`) or every board URL in a text file; asks no board anything |
| `grow_registry [--sources greenhouse,ashby,lever] [--limit N] [--from place] [--matching text] [--any-board] [--write-seed]` | ask candidates' boards, newest-seen first, and register the ones that list a software role (`--any-board`: any posting; `--matching`: a Workday site must answer its own search for the text); prints the registry's width before and after; `--write-seed` rewrites `seed.json` from boards seen somewhere public |
| `match_benchmark --models a:b[,c:d@effort] [--purpose score\|evaluate] [--out f.json]` | how often a model's verdict is the label's, on the 40 postings in `tests/fixtures/match-quality/` (their text comes from `notes/benchmarks/match-quality/` or the mirror); real calls ledgered as `bakeoff`, about $0.03 for the fast scorer; with `LLM_FAKE` on it refuses any model but `fake:score` |
| `watch_now [--email e] [--days N] [--fan-out]` | a watch over the newest postings now instead of after the next scheduled refresh; `--days` widens its window, `--fan-out` starts every watching user's (DEBUG only) |

## Layout

- `backend/foray/`: the Django project. `settings.py`, `settings_test.py`, `env.py` (every
  variable validated at boot; a bad one names itself and exits 1), `api.py` (the one
  `NinjaAPI`), `urls.py`, `asgi.py`, `middleware.py` (Origin check, sliding session, stream
  headers).
- `backend/apps/<app>/`: `core` (the `db()` bridge, `defer_in_tx`, scoping, errors, the router
  factory, the HTTP client, storage, throttles), `accounts`, `llm`, `jobs` (with
  `sources/<source>.py`), `matching`, `resumes`, `applications`, `contacts`, `runs`, `chat`,
  `notifications`, `pages`, `legacy`. An app
  keeps its models and services, and as it needs them `api.py` (its routers), `tasks.py` (its
  procrastinate tasks), `fakes.py` (its `LLM_FAKE` answers) and `management/commands/`.
- `backend/tests/`: pytest, one folder per app, plus `fixtures/`, `goldens/` and `contract/`.
- `src/app/`: pages only (no route handlers, no server actions). `src/components/`: UI.
  `src/lib/api/`: `server.ts` (`django()`, `getMe()` for server components), `client.ts`
  (`api` and `call()` for client components), `types.ts` and `chat-types.ts` (friendly names
  over the generated `schema.d.ts`), `auth.ts`.
- `extension/`: the Chrome extension that fills application forms in the user's own browser
  (#22; [docs/extension.md](docs/extension.md)). Plain JavaScript that Chrome loads as it is:
  `background.js` (the service worker, the only code that holds the token and calls Foray),
  `lib/`, `content/` (what runs in an application page), `options/`, `popup/`, and
  `test/replicas/` (hand-written stand-ins for the boards' forms).
- `notes/` (git-ignored): a clone of the private `dcrew44/foray-notes` repo, for research and
  strategy that isn't public. It may be absent; nothing in the app depends on it.

## Roadmap and bugs

GitHub issues on `dcrew44/Foray` are the tracker; milestones are the roadmap, and `decision`
issues are choices the user makes. Read them with `gh issue list` / `gh issue view <n>`,
close them from pull requests with `Fixes #<n>`, and file follow-ups you notice as issues rather
than leaving them in chat. DESIGN.md keeps the reasons; issues keep the work.

An issue reads like the ones already there:

- **Title**: sentence case, saying what to do ("Wrap short posting fields before they reach a
  model").
- **Body**: a few short paragraphs, no headings: what is wrong, with files and names in
  backticks; the measurements that decide it, when there are any; then the fix. Link related
  issues by number.
- **Last line**: `Done when …`, naming the check that closes it.
- **Labels and milestone**: one or more of `backend`, `frontend`, `llm`, `sources`, `matching`,
  `tooling`, `decision` and GitHub's `bug`, `enhancement`, `documentation`; a milestone when
  the issue belongs to one. Write the body to a file and pass it with `gh issue create
  --body-file`.

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
- **A new model** needs a price in `apps/llm/pricing.py` (an unpriced one costs $0 in the
  ledger) and, when pydantic-ai doesn't know its name, a profile from `reasoning.py`
  (`openai_profile`, `anthropic_profile`). An unknown name silently loses reasoning, effort
  or caching; `gpt-6.1-terra` and `claude-haiku-5-5` both did. Pin what reaches the wire in
  `tests/llm/test_wire.py`, try it without touching `.env` by setting the purpose in the
  process environment (`LLM_SCORE=anthropic:claude-haiku-5-5 pnpm llm:smoke score`), and
  bake it off before configuring it.
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
- **The chat agent asks; the user starts.** A chat tool that starts work which spends passes
  `confirm=True` to its starter, which puts the run behind a `run_start` gate
  (`lifecycle.StartGate`, a kind in `catalogue.START_STEP`); no chat tool answers an approval.
- **Third-party text reaches a model wrapped.** Posting text in a prompt goes through
  `apps/llm/untrusted.py` (`untrusted()`, `one_line()`), and a new chat tool that returns
  posting fields lists their paths in `chat/model_view.POSTING_FIELDS`.
- **A search follows its request.** The search pipeline reads the effective preferences,
  `apply_brief(saved, brief)` (`apps/matching/brief.py`), never the saved preferences
  directly: a request for entry-level roles at one company overrides level, roles, places,
  remote and companies for that run only.
- **A match's apply lane has one rule** (`apps/matching/lanes.py`): `effective_lane(override,
  deep_eval)` is the user's `lane_override`, else the deep review's (`strong` coached, `apply`
  automated, anything else none). Rows and the match page carry it as `lane`; narrow in SQL
  with `lane_expression()`; never derive a lane anywhere else. Only `update_match` writes the
  override, so a re-review cannot move a match the user placed.
- **Runs started from chat report back there.** `emit()` defers a `chat.follow_up` job for
  `FOLLOW_UP_KINDS` (`apps/runs/catalogue.py`, mirrored in `src/lib/chat/follow-up-kinds.ts`;
  a test keeps them equal) when the run has a conversation. Follow-up turns get read-only
  tools only (`FOLLOW_UP_TOOLS` in `apps/chat/tools.py`).
- **A watch runs unasked** (`apps/matching/watch.py`), after every scheduled refresh, so it stays
  quiet: its progress is debug-level, one that finds nothing completes at debug level with
  `announced: 0` (Activity, toasts and Home leave it out), and one that finds something links
  the "New matches" chat as it completes (`lifecycle.complete(conversation_id=…)`) to report
  there. It works only on matches it created, so it never touches a search's.
- **What a watch announces goes to the phone once** (`apps/notifications/`). Queue with
  `outbox.queue_matches(user_id, match_ids)` inside the transaction that announces, before its
  `emit()`: one `notifications` row per (user, match, channel), and nothing for a user with no
  ntfy topic. `notify.send` (deferred then, and swept every minute) sends what is due; quiet
  hours and retries only move `send_after`, and `outbox.claim` leases rows before a send, so
  send through it, never around it. The server is `NTFY_SERVER` and a user supplies only the
  topic; publish with `retries=0` (a repeated POST can deliver twice). Settings' test is a job
  too (`notify.test`, answered by a `notification.test` event): no request waits on ntfy.
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
- **Boards are polled, not just fetched.** `refresh_company` asks a board whether it changed
  (`sources/poll.py`: the validators its last answer came with, then a digest of the body) and
  reads it only when it did; pass `force=True` when a person asked for the read. A board
  adapter is `board_url(slug)`, `parse_board(slug, body)` and `fetch_board(client, slug)`, plus
  `probe_url(slug)` when the source has a cheaper listing that changes with the board. Bump
  `poll.VERSION` when a parser changes what it makes of the same bytes, or unchanged boards
  keep the old reading for up to a day.
- **A board whose listing has no descriptions is a listed adapter** (`workday`,
  `smartrecruiters`, `icims`; `sources.LISTED_ADAPTERS`): `count`, `probe`, `list_board` and
  `read_posting`, one request per posting. `refresh._refresh_listed` reads only postings that
  are new or whose `listing_version` changed, 300 a board and 1,200 a source each pass
  (`client.take`), so never loop over a listing reading every posting. A Workday slug is the
  board's address (`host/site`), found from a link and never guessed; SmartRecruiters boards
  are read only for companies someone added (#69).
- **A board being caught up is not the timed pass's** (#84). A listed board never read, or
  with postings left over from its last listing (`registry.is_catching_up`), is left out of
  `due_companies` and read by `companies.catch_up`, which the scheduled pass defers and which
  runs beside it under `CATCH_UP_GATE`, never `MULTI_BOARD_GATE`. What a pass could not read
  is written to `board_backlogs` (`apps/jobs/backlog.py`) and the next pass goes on from that
  with no probe and no listing; keep it out of `companies.poll_state`, which is read with
  every company.
- **Every company says where its board was learned** (`companies.found_in`: `seed`, `user`,
  `agent`, `jsearch`, `commoncrawl:<crawl>`, `list:<owner>/<repo>`; `discovered_via` is only the
  first). Pass `found_in` to `upsert_company` when a board comes from somewhere new; the shipped
  `seed.json` holds only boards seen in a public index or a licensed list (#28). Boards found
  in bulk go through `board_candidates` and `candidates.grow`, which registers one only after
  its board answered with postings.
- **A form is answered only from saved answers** (`apps/applications/answers.py`, #21):
  `classify(label)` says which shared question a form's label asks, `plan(saved, label,
  options)` gives the answer and where it came from. No model writes an answer about
  sponsorship, pay or years of experience; a voluntary disclosure nobody answered is declined;
  anything else nobody answered is `missing` and waits for the user. A new shared question is
  an `AnswerKind`, a `CATALOGUE` entry and a pattern in `_PATTERNS` (narrower questions first).
  The four work-status answers also filter searches (`work_status`, read by the prefilter).
- **An application is prepared, never sent** (`apps/applications/`, #60). `prepare.start`
  creates the row and defers `application.prepare` in one transaction; the task reads the
  posting's form (`forms.read_form`: Greenhouse publishes its questions, other boards get the
  fields every form has), plans every field (`prepare.plan_form`) and leaves it `ready` or
  `needs_answers`. It is a plain task, not a run: the Apply page shows its result and the
  `application.prepared` event refreshes open pages. After anything that changes saved answers,
  call `prepare.replan(user_id)`. The only request this app makes is the GET that reads a form;
  filling and submitting happen in the user's own browser.
- **A resume variant never stores a fact** (`apps/resumes/variants.py`, #61). A
  `ResumeVariant` is a name, the title keywords that choose it and a `VariantSpec`: the base
  resume's item ids it shows, their order, and its own headline, summary and bullets. Read one
  only through `compose(base, spec)` over `latest_base_resume`, never by copying facts into
  it; an id the base no longer has is dropped, and an unnamed item is left out. A new upload
  keeps the ids of the items it shares with the resume before it (`edits.carry_ids`), so write
  a base version from a fresh reading only through `pipeline._save_import`. An application
  asks `resume_for(user_id, title, variant_id)` (deterministic: whole-word keyword matches in
  the title, then the default variant, then the base resume; no model) and prints
  `resume_row(user_id, chosen.variant)`. Rendering goes through `pipeline.render_variant`,
  which writes the composed snapshot (a `resumes` row of kind `variant`) and renders that; a
  variant's PDF is named and keyed for the variant. A refusal the user reads is a
  `VariantRefused`. The editor's pure helpers (`src/lib/variants.ts`) mirror `clean_spec`.
- **An outcome number comes with its count** (`apps/applications/outcomes.py`, #65, #6).
  Django decides when a number is too small to be a rate: `None` under `MIN_DENOMINATOR`, no
  bucket rates before `RATES_AFTER` recorded applications, no 90th percentile under `P90_MIN`.
  The frontend words what it was sent and never divides. A stage a match "reached" is only what
  its current stage proves (`reached()`), because nothing records a stage move; say what a new
  number cannot know rather than guessing it. A new split is one more key in `funnel()`'s
  grouped count, not another query.
- **Contacts come from a file the user exported, and go nowhere** (`apps/contacts/`, #63).
  `parse.parse_contacts` reads LinkedIn's Connections.csv (or the archive it comes in) and any
  CSV with a name column; it is pure, and its fixtures hold made-up people only.
  `importer.import_contacts` runs in the request, makes no request of its own and defers
  nothing; a person is one row per `dedupe_key`, so the same file again writes nothing. A
  contact is linked to a registered company through `link.CompanyIndex` (the name as the mirror
  normalises it, then a work email's domain, never a free-mail one), and `link.rematch()` links
  the rest when their company is registered later. No LinkedIn request of any kind, ever. The
  one thing that leaves is a company *name*, and only when the user presses "Look for the
  boards" (`page.start_lookup`, the existing `company_discovery` run): never start that
  unasked, and never put a person's field in it.
- **A match says who the user knows there** (`apps/contacts/known.py`, #64). `known_at` answers
  for any number of postings in one query (the contact's linked company, or its company name's
  key against the posting's). Rows a page or card shows get `knows` from
  `matching.queries.with_known(user_id, rows)`; rows a pipeline only reads do not ask. A watch's
  message counts the announced matches where the user knows someone, its chat update names the
  people (`chat.follow_up.known_roles`, wrapped with `untrusted()`), and the phone notification
  says only how many: a name never goes to a notification server. With nobody known, each of
  the three is byte for byte what it was, and tests pin both cases. Anything new that tells a
  model about a contact wraps the name and position the same way.
- **A note to a contact is drafted, never sent** (`apps/contacts/outreach.py`, #25).
  `start_note(user_id, match_id, contact_id)` starts an `outreach` run (one model call is
  still a run: `run_step` gives it the ledger, the timeout, the budget's sentence and a
  Stop), and the step writes `outreach_notes` and completes the run in one transaction.
  The prompt is `note_instructions` (the rules and the user's own resume) plus `note_prompt`
  (the contact, the posting and the review, each through `untrusted()`, and one line Foray
  writes about how they know each other, which says only what the data says). Both are
  pinned in `tests/goldens/prompts/outreach/`; re-pin with `FORAY_UPDATE_SNAPSHOTS=1` when
  you change them on purpose. Keep every field of a contact out of a run's input, output
  and event messages (the chat agent's tools read those), and add nothing that sends a
  note or contacts anyone: the user copies it and sends it themself.
- **The browser extension signs in with a token, and the server decides what it may do**
  (`apps/applications/extension.py`, #22, #23, #24, #40). Its routes live under
  `/api/extension/`, take only `Authorization: Bearer` (never a session; a request carrying
  the session cookie is refused) and are exempt from the Origin check for that reason
  (`middleware.is_extension_call`). The extension is given `payload()` and nothing else:
  `blocked` (the limits in Settings: per employer in 60 days, and per day) and `may_submit`
  (off unless the user's `autoSubmit` is on, the match is in the automated lane and every
  answer's source is in `OWN_ANSWERS`) are worked out here, never in the extension, and "sent
  on its own" is recorded only when the server's rule allowed it. A new answer source is not
  auto-submittable until it is added to `OWN_ANSWERS` on purpose. New routes there go in the
  token sweep in `tests/applications/test_extension.py`.
- **The extension fills; the user submits** (`extension/`, #22). It puts in only the answers
  Foray planned (a field whose `value` is null is left alone), never ticks an agreement, never
  touches a CAPTCHA, a sign-in or an account form, and presses submit only when the payload
  says `maySubmit` and none of those is on the page. No build step, no dependency and no
  remote code: content scripts are classic scripts on one `Foray` object in the isolated
  world, the worker and the two pages are ES modules. The token is read only in
  `lib/api.js`, by the worker; a content script asks by message and is answered only for
  the page Chrome says it is in. A control gets a strategy in `content/controls.js`, a board
  an adapter in `content/boards.js` with its host in the manifest, a replica and a test.
  **Never fill, upload to or submit a real application form to test it**: the tests
  (`backend/tests/extension/`, `slow`) load the extension into Playwright's Chromium against
  the replicas and a fake Foray, with every real host unresolvable.
- **What a posting asks is read once** (`apps/jobs/facts.py`): level, years, work authorization
  and clearance, by patterns, at ingest. Filter on the `jobs` columns; do not ask a model for
  them. When the reading changes, bump `facts.VERSION` (the scheduled refresh then re-reads
  the mirror a batch at a time) and run `manage.py facts_benchmark --show`: it must stay above
  90% on level and years, and a change tuned on the `fresh` set stops that set being a test.
- **A posting's warning signs are worked out on read** (`apps/jobs/legitimacy.py`): closed, an
  aggregator's apply link that leaves the employer, reposted, open a long time. Nothing is
  stored and no model is asked. A new signal is a kind in `SignalKind`, a pure rule and its
  place in `signals()`; whatever a rule needs beyond the posting's own row joins the one query
  in `read_context`, because the match page may spend only one on them. A new board adapter's
  job links must pass `is_board_link` (`tests/jobs/test_legitimacy.py` fails until one is
  listed there and known).
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
- **A match leads with its verdict.** One vocabulary names a fit: the recommendation labels,
  or "Not reviewed" for a match with only a triage score (`verdictLabel`, `VerdictPill`).
  `fitLevel` steps the pine scale on the rubric bands and takes whether the score was
  reviewed. The line of why and the blocker come from Django on every row and on the match
  page (`reason`, `blocker`; `apps/matching/verdict.py`) and are drawn by `Why` and `Blocker`
  (`src/components/matches/verdict.tsx`); never derive them in the frontend.
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

- **Rehearse on the rehearsal stack.** Anything that writes (a search, a watch, an
  application, a migration you have not merged) runs under `pnpm rehearsal`
  (`preview_start` with `foray-rehearsal`), never against the everyday database: there the
  worker would score the owner's real matches with fake models. The everyday stack
  (`foray-web`) is for looking at real data with merged code. Both answer on :3000, so the
  pane's session from one is unknown to the other: its first request gets a 401, which clears
  the cookie. Set a session again after switching (`pnpm rehearsal -- pnpm dev:session …`
  there, `pnpm dev:session you@example.com` back on the everyday stack).
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
- **One worker per database** (`apps/runs/worker_lock.py`): a second worker waits behind the
  first. If `pnpm worker:up` is running, the worker inside `dev:all` logs "another worker is
  running on this database; waiting for it to stop" and does nothing, so merged code runs
  the everyday stack's jobs. The rehearsal database has its own lock and its own worker.
- The watch-mode worker restarts on every `.py` save and interrupts whatever it was running;
  the reaper fails such runs ("Interrupted…") within about two minutes, so re-run after edits
  settle. A worker killed with `kill -9` is not restarted until the next save.
- A model named on a command line (`llm:bakeoff --models …`) is called for real whatever
  `LLM_FAKE` says: the fake only stands in for the models the purposes are configured with.
  `match_benchmark` refuses that under `LLM_FAKE`; `llm:bakeoff` does not, so never run it on
  the rehearsal stack expecting it to be free.
- `LLM_FAKE=1` (with `DEBUG=true`) runs every model as a deterministic fake at $0;
  `LLM_FAKE_DELAY_MS` slows it enough to press Stop, and `LLM_FAKE_FAIL=<purpose>` makes one
  fail. Set them in the process environment for a rehearsal rather than editing `.env`.
- Phone notifications are real in a rehearsal: only the models are fake, so a user who has
  saved an ntfy topic gets what a watch announces, on `NTFY_SERVER`. Give a test user a topic
  of your own, or none. `pnpm rehearsal manage watch_now --email …` starts a watch now.

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
  (it needs `node` and `pnpm install`). Tests marked `slow` need a browser: they render PDFs
  with a local Chromium or Chrome, and `tests/extension/` loads the extension into
  Playwright's own Chromium (`uv run playwright install chromium`; skipped without it).
  `-m "not slow"` skips them. Playwright's sync API keeps an event loop running on its thread,
  so open it per test (the `chromium` fixture), never per session, and read the database from
  another thread while it is open (`tests/extension/test_against_django.py`).
- `FORAY_REGISTER_STRICT=1` makes `tests/test_register.py` fail when a test that DESIGN.md's
  invariant register names does not exist. Run the full suite with it before committing.
- **CI** (`.github/workflows/`): `ci.yml` runs `check` (lint, typecheck, both suites with
  `FORAY_REGISTER_STRICT=1` and `-m "not slow"`, then the `api:types` drift check), `build`
  and `slow` on every pull request, push to `main` and nightly; `docker.yml` builds both image
  targets when an image input changes. CI has no secrets and no `.env`, so a step that boots
  `settings.py` outside pytest sets its own variables (see the `api:types` step).
- Vitest covers only pure helpers: the frontend's (`src/test/unit/*.test.ts`: formatting, the
  sign-in `next` guard, the upload size check) and the extension's
  (`extension/test/unit/*.test.js`: label and option matching, the address on its options
  page).

## API drift warning

pydantic-ai 2.54, django-ninja 1.7, Django 6.1, Next 16 and AI SDK 7 all differ from model
training data. Read the installed source (`backend/.venv/lib/python3.14/site-packages/…`,
`node_modules/…`) before relying on an API you remember. Notably:

- **pydantic-ai 2.54**: `Agent(model, instructions=…, output_type=…, deps_type=…)`;
  `run_stream_events(...)` is an async context manager, not an iterator; models compose as
  `WrapperModel` subclasses; the unified `thinking` setting is silently dropped for DeepSeek
  (which is why `reasoning.py` sends its own switch); a model's profile is picked by name
  prefix, so one newer than the pin gets the defaults (see "A new model"); the UI stream
  comes from `pydantic_ai.ui.vercel_ai` (`VercelAIEventStream`, subclassed in
  `apps/chat/uistream.py`).
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

Build autonomously on a branch, one per piece of work (`claude/<name>`), and open a pull
request once its "Done" check is actually verified; never commit to `main` directly. Stop
and ask only for things the user must do (API keys, starting Docker Desktop, creating
accounts) or at a genuine fork not covered by the plan.
