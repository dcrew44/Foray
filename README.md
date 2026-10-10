# Foray

An AI job-search agent. Upload your resume, tell it what you want in plain words, and it
does the legwork: searches ~200 company job boards plus an aggregator, scores every
posting against your actual resume, deep-reviews the best with evidence quoted from your
experience, proposes a shortlist, and tailors your resume to a specific role, checking
every claim before it renders a PDF.

You stay in charge of spending: by default a search pauses before the deep review, showing
what it will cost, and again before shortlisting. Autopilot skips both pauses. Model calls
stop for the month at a budget you set (`LLM_MONTHLY_BUDGET_USD`, $90 by default).

See [DESIGN.md](DESIGN.md) for the settled decisions and architecture, and
[CLAUDE.md](CLAUDE.md) for working conventions.

## What's in the app

| Page | What it's for |
|---|---|
| **Home** | Status at a glance: decisions waiting on you, searches in flight, your best matches, and a box to ask Foray anything. New accounts get a three-step setup here. |
| **Chat** | The agent. Ask for roles, have it explain a fit, move jobs on your board, add companies, start a deep review or a tailored resume. Background work appears as live cards, and the agent reports back in the chat when that work needs you or finishes. |
| **Matches** | Your pipeline as a drag-and-drop board (Found → Shortlisted → Applied → Interviewing → Offer), or as a filterable list. |
| **Match page** | Why a role fits: the deep review's evidence and gaps, the posting, a tailored resume with its change log and fact-check, and your notes. |
| **Activity** | Every run and every step the agents took. |
| **Profile** | Your resume, as the structured facts every model judges you by, and what you're looking for. |
| **Companies** | The job boards Foray watches. Add one by name, domain or board URL. |
| **Settings** | Autonomy, score threshold, deep-review count, model spend this month, model configuration, worker status, appearance (dark by default, light, or match your system). |

## How it runs

Four processes, the same in development and production:

- **Postgres 18**, in Docker;
- **web**: Next.js 16 on :3000, which renders the pages and forwards every `/api/*` request;
- **api**: Django 6.1 on uvicorn on :8000, which owns the data, sign-in and the chat stream;
- **worker**: one procrastinate process that runs searches, scoring, tailoring, PDF rendering
  and board refreshes in the background.

The browser only ever talks to :3000.

## Quickstart

Requires Node 24+ and pnpm 10, Python 3.14 and [uv](https://docs.astral.sh/uv/), Docker
Desktop, and Google Chrome (used headlessly to render resume PDFs; or install Playwright's
Chromium with `cd backend && uv run playwright install chromium`; the worker container
brings its own).

```bash
pnpm install
(cd backend && uv sync)  # the Python backend's virtualenv
cp .env.example .env     # set SECRET_KEY, LLM_DEFAULT and that provider's API key
pnpm db:up               # Postgres 18 in Docker
pnpm db:create           # the foray_app database
pnpm db:migrate
pnpm seed:companies      # ~200 verified company job boards (add --fetch to fill the mirror now)
pnpm dev:all             # web on :3000, api on :8000, and the background worker
```

Open http://localhost:3000, create an account, and drop your resume on the home page.

The worker does all the heavy lifting. If a search seems stuck, the sidebar says
"Worker offline" and tells you to run `pnpm dev:worker`.

### Bringing over data from the TypeScript version

Foray ran on a TypeScript server (Next.js plus a Node worker) until October 2026, with its
data in a database named `foray`. The import copies everything (your account and password,
preferences, resumes, documents, matches, runs, the ledger) into `foray_app`, all or
nothing, and never writes to the old database or its files. Instead of `pnpm seed:companies`:

```bash
# in .env: LEGACY_DATABASE_URL=…/foray, LEGACY_STORAGE_DIR=<the old app's data/ folder>
pnpm db:backup-legacy    # a dump, a file manifest and a marker in BACKUP_DIR
pnpm db:import-legacy    # every check runs inside the import's transaction
pnpm db:verify-import    # re-runs the checks
pnpm db:verify-import --password-prompt   # in your own terminal: checks your old password
pnpm db:mark-cutover     # this import is now the real data
```

Mark the cutover before you start using the app. Until then `import_legacy --replace` and
`reset_app_db` still work, and either one discards everything in `foray_app`, including
anything you made since the import; once it is marked, both refuse. Sessions are not carried
over: you sign in once with the same email and password.

### Models and cost

There is no default vendor. Set `LLM_DEFAULT=provider:model` and override per purpose
(`LLM_CHAT`, `LLM_SCORE`, `LLM_EVALUATE`, `LLM_TAILOR`, …); `.env.example` carries a price
sheet. Every call is recorded in the `llm_calls` ledger, and **Settings → Spend this
month** breaks it down by purpose and model. `pnpm llm:smoke` makes one call per purpose
to check your keys.

Measured costs with the current OpenAI configuration (GPT-5.6 Luna for bulk work, Terra
for judgment): scoring is well under a cent per posting; each deep review is about
2.5 cents, so a search that deep-reviews 15 roles costs roughly 40 cents all in; a
tailored resume is a few cents to ten.

## Commands

| Command | What it does |
|---|---|
| `pnpm dev:all` | web, api and worker in watch mode |
| `pnpm db:up` / `db:create` / `db:migrate` | start Postgres / create `foray_app` / apply Django migrations |
| `pnpm seed:companies` | load the verified company boards |
| `pnpm test` | Vitest (frontend helpers) and pytest (the backend, on its own test database) |
| `pnpm lint` / `typecheck` | ESLint and ruff / `tsc --noEmit` and pyright |
| `pnpm check` | everything above, plus a check that the generated API types are current |
| `pnpm llm:smoke` | one call per configured purpose, to check keys and pricing |
| `pnpm llm:bakeoff` | compare models on fixed scoring fixtures (cost, latency, agreement) |
| `pnpm auth:set-password <email>` | set a forgotten password at a hidden prompt |
| `pnpm docker:prod` | Postgres, migrate, api, worker and web from the production images |

## Status

Phase 1 (find, rank, tailor) is complete, and runs on the Django backend since October 2026.
Next: a browser agent that fills in and submits applications with your approval (Phase 2),
then recruiter outreach over Gmail (Phase 3).
