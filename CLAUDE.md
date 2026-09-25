# CLAUDE.md

Working notes for Foray. Read [DESIGN.md](DESIGN.md) for *why*; this file is *how*.

## Commands

| Command | What it does |
|---|---|
| `pnpm dev` | Next.js dev server on :3000 (`foray-web` in `.claude/launch.json`) |
| `pnpm dev:worker` | `tsx watch src/worker/main.ts` |
| `pnpm dev:all` | both, concurrently |
| `pnpm build` | production build; run it before committing UI or route changes |
| `pnpm db:up` / `db:down` | Postgres 18 in Docker (`docker compose up -d db`) / stop it |
| `pnpm db:generate` / `db:migrate` / `db:studio` | Drizzle migrations and studio |
| `pnpm auth:generate` | regenerate Better Auth tables into the Drizzle schema |
| `pnpm auth:set-password <email>` | set a forgotten password at a hidden prompt (no mail transport yet) |
| `pnpm seed:companies` | load `src/lib/companies/seed.json` into `companies` |
| `pnpm fetch:board` / `verify:boards` | print what an adapter makes of one board / check that board slugs are real (neither touches the database) |
| `pnpm test` / `test:watch` / `typecheck` / `lint` | Vitest, `tsc --noEmit`, ESLint |
| `pnpm llm:smoke` | one call per configured purpose, to check keys and pricing |
| `pnpm llm:bakeoff` | compare models on fixed scoring fixtures |
| `pnpm docker:prod` | `docker compose --profile prod up --build` |

## Boundaries (enforced by ESLint `no-restricted-imports`)

- `src/app/**` and `src/components/**` must not import `playwright` or `src/worker/**`.
- `src/worker/**` must not import `src/app/**`.
- Both import `src/lib/**`. Shared logic goes there, not into either entry point.

## Conventions

- **Node 24+, ESM only** (`"type": "module"`), TypeScript pinned to `~5.9`.
- **Ask for an LLM purpose, never a model**: `model('score')`, not a model id. Vendor
  quirks (reasoning knobs, unsupported `temperature`, caching) live only in
  `src/lib/llm/models.ts`. The `llm_calls` ledger is middleware on the model that
  `model()` returns (`llm/ledger.ts`), so agent loops are recorded too; attribute a call
  to a user or run with `withLlmContext`. Direct calls use `callLLM` or
  `generateStructured` for the rate limiter and per-purpose reasoning effort.
- **User scoping**: every query module in `src/lib/db/queries/` takes `userId` as its
  first argument. No unscoped read path to per-user data.
- **Job ids** are `source:company_slug:external_id`; career pages use
  `web:<slug>:<sha1(url)>`.
- **Never rewrite `description_raw`.** Cleaning produces `description_text`.
- **Heavy work is a pg-boss job**, never inline in a request handler.
- **Long-running steps emit `agent_events`** so the activity feed shows progress.
- Approval gates use the continuation pattern described in DESIGN.md — a handler that
  needs a decision writes the approval and returns; deciding enqueues the continuation.
- **A search follows its request.** The search pipeline reads the effective preferences,
  `applyBrief(saved, brief)` (`lib/ranking/brief.ts`), never the saved preferences
  directly: a request for entry-level roles at one company overrides level, roles,
  places, remote and companies for that run only.
- **Runs started from chat report back there.** `emit()` queues a chat follow-up for the
  kinds in `lib/chat/follow-up-kinds.ts` when the run has a `conversationId`. A new run
  kind should end with `run.completed` or `run.failed` (or add its own final kinds
  there), or its chat goes quiet. Follow-up turns get read-only tools only
  (`FOLLOW_UP_TOOLS` in `agents/chat-agent.ts`).

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
- shadcn components here are the Base UI flavour: use the `render` prop, not `asChild`.

## Driving the app in a browser

Verify UI in the built-in browser pane (`preview_start` with `foray-web`) so the user
can watch; headless Playwright scripts are for extra screenshots, not the only check.

- Never type a password. Mint a dev session for an existing local user:
  `pnpm tsx scripts/dev-session.ts you@example.com` prints
  `cookie: better-auth.session_token=…`. Drop the `cookie: ` prefix and set the rest
  with `document.cookie` in the pane. Do not revoke a session the pane is still using.
- Typing and clicking need the pane on screen, and a click by a stale `ref` can miss:
  focus the composer with JavaScript and check its value before pressing Enter.
  While the pane is hidden, screenshots show an old frame and the app polls every
  6 to 30 seconds, so read state with `get_page_text` instead.
- Don't edit files while typing into the pane: Fast Refresh can reset the composer.
- The watch-mode worker restarts on every save and interrupts whatever it was running;
  the reaper marks such runs failed at boot, so re-run after edits settle.

## Tests

Vitest runs against the dev database. Database tests insert a throwaway user and
delete it afterwards, and fake the queue with `vi.mock("@/lib/queue/send")`. The
watch-mode worker shares that database, and its reaper can fail a test's "running" run
if it restarts mid-suite; re-run before chasing such a failure.

## API drift warning

Next 16, AI SDK 7, and Better Auth 1.7 all differ from model training data. Before
writing AI SDK code, read the vendored skills in `.agents/skills/` (`ai-sdk` and
`migrate-ai-sdk-v6-to-v7`, symlinked into `.claude/skills/`), and heed `AGENTS.md` —
Next re-writes its block there and points at `node_modules/next/dist/docs/`. Notably:
`ToolLoopAgent` takes `instructions` (not `system`), `stopWhen: isStepCount(n)` and
`activeTools`; structured output via `generateText({ output: Output.object({ schema }) })`;
chat streams with `createAgentUIStreamResponse()`, and a turn with no client (a
follow-up) runs with `createAgentUIStream()` plus `consumeStream()`; tool approval via
`toolApproval`.

## Workflow

Build autonomously; commit each working step to `main` with the step's "Done" check
actually verified. Stop and ask only for things the user must do (API keys, starting
Docker Desktop, creating accounts) or at a genuine fork not covered by the plan.
