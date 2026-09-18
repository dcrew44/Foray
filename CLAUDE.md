# CLAUDE.md

Working notes for Foray. Read [DESIGN.md](DESIGN.md) for *why*; this file is *how*.

## Commands

| Command | What it does |
|---|---|
| `pnpm dev` | Next.js dev server on :3000 |
| `pnpm dev:worker` | `tsx watch src/worker/main.ts` |
| `pnpm dev:all` | both, concurrently |
| `pnpm db:up` | Postgres 18 in Docker (`docker compose up -d db`) |
| `pnpm db:generate` / `db:migrate` / `db:studio` | Drizzle migrations and studio |
| `pnpm auth:generate` | regenerate Better Auth tables into the Drizzle schema |
| `pnpm seed:companies` | load `src/lib/companies/seed.json` into `companies` |
| `pnpm test` / `typecheck` / `lint` | Vitest, `tsc --noEmit`, ESLint |
| `pnpm docker:prod` | `docker compose --profile prod up --build` |

## Boundaries (enforced by ESLint `no-restricted-imports`)

- `src/app/**` must not import `playwright` or `src/worker/**`.
- `src/worker/**` must not import `src/app/**`.
- Both import `src/lib/**`. Shared logic goes there, not into either entry point.

## Conventions

- **Node 24+, ESM only** (`"type": "module"`), TypeScript pinned to `~5.9`.
- **Ask for an LLM purpose, never a model**: `model('score')`, not a model id. Vendor
  quirks (reasoning knobs, unsupported `temperature`, caching) live only in
  `src/lib/llm/models.ts`. Every call goes through `src/lib/llm/call.ts` so the
  `llm_calls` ledger stays complete.
- **User scoping**: every query module in `src/lib/db/queries/` takes `userId` as its
  first argument. No unscoped read path to per-user data.
- **Job ids** are `source:company_slug:external_id`; career pages use
  `web:<slug>:<sha1(url)>`.
- **Never rewrite `description_raw`.** Cleaning produces `description_text`.
- **Heavy work is a pg-boss job**, never inline in a request handler.
- **Long-running steps emit `agent_events`** so the activity feed shows progress.
- Approval gates use the continuation pattern described in DESIGN.md — a handler that
  needs a decision writes the approval and returns; deciding enqueues the continuation.

## API drift warning

Next 16, AI SDK 7, and Better Auth 1.7 all differ from model training data. Before
writing AI SDK code, read the vendored skills in `.agents/skills/` (`ai-sdk` and
`migrate-ai-sdk-v6-to-v7`, symlinked into `.claude/skills/`), and heed `AGENTS.md` —
Next re-writes its block there and points at `node_modules/next/dist/docs/`. Notably:
`ToolLoopAgent` takes `instructions` (not `system`), `stopWhen: isStepCount(n)`,
structured output via `generateText({ output: Output.object({ schema }) })`,
`createUIMessageStreamResponse()`, tool approval via `toolApproval`.

## Workflow

Build autonomously; commit each working step to `main` with the step's "Done" check
actually verified. Stop and ask only for things the user must do (API keys, starting
Docker Desktop, creating accounts) or at a genuine fork not covered by the plan.
