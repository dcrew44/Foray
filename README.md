# Foray

An AI job-search agent. Upload your resume, tell it what you want in plain words, and it
does the legwork: searches ~200 company job boards plus an aggregator, scores every
posting against your actual resume, deep-reviews the best with evidence quoted from your
experience, proposes a shortlist, and tailors your resume to a specific role, checking
every claim before it renders a PDF.

You stay in charge of spending: by default a search pauses before the deep review, showing
what it will cost, and again before shortlisting. Autopilot skips both pauses.

See [DESIGN.md](DESIGN.md) for the settled decisions and architecture, and
[CLAUDE.md](CLAUDE.md) for working conventions.

## What's in the app

| Page | What it's for |
|---|---|
| **Home** | Status at a glance: decisions waiting on you, searches in flight, your best matches, and a box to ask Foray anything. New accounts get a three-step setup here. |
| **Chat** | The agent. Ask for roles, have it explain a fit, move jobs on your board, start a deep review or a tailored resume. Background work appears as live cards, and the agent reports back in the chat when that work needs you or finishes. |
| **Matches** | Your pipeline as a drag-and-drop board (Found → Shortlisted → Applied → Interviewing → Offer), or as a filterable list. |
| **Match page** | Why a role fits: the deep review's evidence and gaps, the posting, a tailored resume with its change log and fact-check, and your notes. |
| **Activity** | Every run and every step the agents took. |
| **Profile** | Your resume, as the structured facts every model judges you by, and what you're looking for. |
| **Companies** | The job boards Foray watches. Add one by name, domain or board URL. |
| **Settings** | Autonomy, score threshold, deep-review count, model spend this month, model configuration, worker status, appearance (dark by default, light, or match your system). |

## Quickstart

Requires Node 24+, pnpm 10, Docker Desktop, and Google Chrome (used headlessly to render
resume PDFs; in the worker container the Playwright image provides Chromium).

```bash
pnpm install
cp .env.example .env     # set BETTER_AUTH_SECRET and at least one LLM provider key + LLM_DEFAULT
pnpm db:up               # Postgres 18 in Docker
pnpm db:migrate
pnpm seed:companies      # ~200 verified company job boards
pnpm dev:all             # Next.js on :3000 + the background worker
```

Open http://localhost:3000, create an account, and drop your resume on the home page.

The worker does all the heavy lifting. If a search seems stuck, the sidebar says
"Worker offline" and tells you to run `pnpm dev:worker`.

### Models and cost

There is no default vendor. Set `LLM_DEFAULT=provider:model` and override per purpose
(`LLM_CHAT`, `LLM_SCORE`, `LLM_EVALUATE`, `LLM_TAILOR`, …); `.env.example` carries a price
sheet. Every call is recorded in the `llm_calls` ledger, and **Settings → Spend this
month** breaks it down by purpose and model.

Measured costs with the current OpenAI configuration (GPT-5.6 Luna for bulk work, Terra
for judgment): scoring is well under a cent per posting; each deep review is about
2.5 cents, so a search that deep-reviews 15 roles costs roughly 40 cents all in; a
tailored resume is a few cents to ten.

## Commands

| Command | What it does |
|---|---|
| `pnpm dev:all` | Next.js dev server + worker (watch mode) |
| `pnpm test` / `typecheck` / `lint` | Vitest, `tsc --noEmit`, ESLint |
| `pnpm db:generate` / `db:migrate` / `db:studio` | Drizzle migrations and studio |
| `pnpm llm:smoke` | One call per configured purpose, to check keys and pricing |
| `pnpm llm:bakeoff` | Compare models on fixed scoring fixtures (cost, latency, agreement) |
| `pnpm docker:prod` | Postgres + migrate + web + worker from production images |

## Status

Phase 1 (find, rank, tailor) is complete. Next: a browser agent that fills in and submits
applications with your approval (Phase 2), then recruiter outreach over Gmail (Phase 3).
