# Completeness critique: gaps, fills and cross-area contract checks

Repo root: `/Users/haydenbarolette/Projects/JobSearchAgent/.claude/worktrees/middleware-django-migration-389b1a`
(HEAD `f82e21c`). All `file:line` references below are relative to it and were re-read for this
pass. The other map files sit next to this one (`http-surface.md`, `next-frontend.md`, `auth.md`,
`database.md`, `llm.md`, `chat-agent.md`, `worker.md`, `tests-scripts-infra.md`).

Method:
1. `git ls-files` (253 files excluding `src/test/fixtures/**` and `src/components/ui/**`). For each
   file I searched every map for its full path, its path without `src/`, its last two path segments,
   and then its file name. The full table is the appendix (section 9).
2. Grep audit of server-only behaviour: `process.env`, `setTimeout`/`setInterval`,
   `AsyncLocalStorage`, `fs`, `child_process`, external `fetch`, `"use server"`, `server-only`,
   `revalidatePath`/`revalidateTag`/`unstable_cache`, `headers()`/`cookies()`, Next route-segment
   config, `React.cache`, id/hash generation, locale formatting, logging (section 2).
3. Cross-checked the contracts that span maps: event kinds emitted against UI consumers, run
   `stats`/`output` keys against UI readers, the live snapshot against `LiveProvider`, queue names and
   policies, tool outputs against `ToolPart`, and the claims in DESIGN.md against the code (sections 3-6).

---

## 0. Short list (what was missing and is filled below)

| # | Gap | Where filled |
|---|---|---|
| G1 | No map indexes DESIGN.md's 17 invariants as a whole, and several DESIGN.md claims are not true of the code | 3.1 |
| G2 | No single emitter-to-consumer matrix for `agent_events` kinds. Three final or important kinds have no toast and no refresh hook | 3.2 |
| G3 | No writer-to-reader contract for run `stats`/`output` keys. The UI reads keys and phases the worker never writes | 3.3 |
| G4 | **Zombie `queued` runs.** Nothing ever reaps a `queued` run. A lost job blocks `activeSearch` and `rank_deep` reuse forever. This is a cutover hazard | 3.4 |
| G5 | **Live-feed cursor race.** A bigserial id is allocated at insert but becomes visible at commit, so events can be skipped. The risk is much higher under Django's transaction patterns | 3.5 |
| G6 | LLM cost attribution quirks: careers-page extraction is billed to the company's adder, and follow-ups and chat turns carry no `run_id` | 3.6 |
| G7 | Exact `OnboardingState`/`InterviewProgress` JSON shape. Four pages and the chat suggestions depend on it, and `resume` there comes from `profiles.data`, not `resumes(base)` | 3.7 |
| G8 | One consolidated inventory of server timers, sleeps, timeouts and windows | 3.8 |
| G9 | Server logging inventory (prefixes) for ops parity | 3.9 |
| G10 | No retention or cleanup jobs exist anywhere, and no app-level spend rate limits | 3.10 |
| G11 | Exact `revalidatePath` table with the conditions under which each call fires (14 of 16 actions, not 11) | 3.11 |
| G12 | Small edge behaviours no map states: heartbeat clock skew, a "nothing stale" refresh that never answers the button, a search send failure that leaves a zombie and shows an error page, and the worker-offline banner copy | 3.12 |
| G13 | Files with no map coverage: three historical drizzle snapshots and `icon.svg` (none is server code) | 1 |

---

## 1. File coverage result

Every server-relevant tracked file is covered by at least one map (appendix, section 9). Files
with **no** mention in any map:

| File | What it is | Port impact |
|---|---|---|
| `drizzle/meta/0000_snapshot.json`, `0001_snapshot.json`, `0002_snapshot.json` | drizzle-kit's schema snapshots after migrations 0000-0002. They are used only by `drizzle-kit generate` to diff the next migration. `database.md` verified the live DB against `0003_snapshot.json` and the 4 SQL files. | None. Django migrations replace drizzle-kit. Keep the drizzle folder only if `__drizzle_migrations` history matters to you; Django never reads it. |
| `src/app/icon.svg` | Favicon, served by Next's file convention. | Frontend asset; it stays in Next. |

Files covered only by file name (a table row or bullet with no full path): the 15 `src/test/unit/*.test.ts`
files (`tests-scripts-infra.md` §2), and roughly 20 `src/components/**` files (`next-frontend.md` §5).
That coverage is adequate; I spot-checked each.

Partially covered: **`DESIGN.md`**. It is cited about 15 times for individual invariants (4-8, 10-13, 15,
16), but invariants 1, 2, 3, 9, 14 and 17 are never cited by number, and its "Interfaces", "Known risks"
and "Phase plan" sections are not reconciled with the code. Filled in 3.1.

`.agents/skills/*`, `.claude/skills/*` (symlinks), `skills-lock.json`, `AGENTS.md` and `README.md` are
mentioned in `tests-scripts-infra.md` §8. They are developer tooling and docs, with no runtime behaviour.

---

## 2. Grep audit of server-only behaviour (every site, and which map covers it)

| Category | Sites (file:line) | Covered by |
|---|---|---|
| `process.env` | `src/env.ts:98` (`SKIP_ENV_VALIDATION`); `src/lib/llm/models.ts:143` (`LLM_REASONING_<PURPOSE>`, read at call time); `src/lib/load-dotenv.ts:11` (`process.loadEnvFile()`). All other config goes through `src/env.ts` (`@/env` imported by `sign-in/page.tsx`, `auth/index.ts`, `companies/discovery.ts`, `db/index.ts`, `llm/limiter.ts`, `llm/models.ts`, `llm/registry.ts`, `queue/boss.ts`, `sources/jsearch.ts`, `storage/index.ts`, `worker/main.ts`, `worker/pdf.ts`, plus scripts). Provider SDKs also read their `*_API_KEY` from `process.env` implicitly. | tests-scripts-infra §6.1 (complete table), llm §1 |
| `setTimeout`/`setInterval` (server) | `llm/limiter.ts:59` (token-bucket wait, minimum 25 ms); `chat/follow-up.ts:101` (1.5 s poll while a reply is in flight); `sources/http.ts:42` (retry backoff); `worker/pdf.ts:172` (close-when-idle, 25 s, unref'd); `worker/main.ts:131` (heartbeat, 10 s). | llm §5, chat-agent §6.2, worker §13.2/§15/§1.1. Consolidated in 3.8 |
| `AsyncLocalStorage` | `llm/context.ts:1,17`; set at `llm/call.ts:50`, `app/api/chat/route.ts:66`, `chat/follow-up.ts:136` | llm §4, chat-agent §7 |
| fs | `storage/index.ts:2,59-69` (mkdir/writeFile/readFile/unlink under `STORAGE_DIR`); `scripts/seed-companies.ts:31` (seed.json from cwd); `scripts/verify-boards.ts:73` (`--file`); tests read fixtures | worker §16, tests-scripts-infra §4 |
| `child_process` | none | n/a |
| Outbound HTTP | `sources/http.ts:77` (all ATS, JSearch and careers-page fetches); `companies/discovery.ts:163` (Brave search, raw fetch, no UA); LLM providers through the AI SDK. Hosts: `boards-api.greenhouse.io` (`greenhouse.ts:43`), `api.lever.co` (`lever.ts:56`), `api.ashbyhq.com` (`ashby.ts:59`), `jsearch.p.rapidapi.com` (`jsearch.ts` HOST), `api.search.brave.com` (`discovery.ts:159`), arbitrary company sites (discovery and careers pages), Google OAuth (Better Auth, disabled). | worker §13-§18, llm §2 |
| `"use server"` | the 8 files in `src/app/actions/*.ts` | http-surface §4, next-frontend §6 |
| `server-only` | **none, by design** (`storage/index.ts:10-14`: the package throws outside an RSC bundler, and `src/lib` is shared with the worker and scripts) | (noted here) |
| `revalidatePath` | 14 actions (table in 3.11). There are no `revalidateTag` or `unstable_cache` calls. | http-surface §4, next-frontend §6 |
| `headers()` / `cookies()` | only `lib/auth/session.ts:11` (`headers()` passed to Better Auth) | auth §9 |
| Route segment config (`dynamic`, `revalidate`, `runtime`, `maxDuration`), `React.cache`, `after()` | none | http-surface §0.3 |
| `redirect` / `notFound` | `app/page.tsx:7`, `onboarding/page.tsx:8`, `sign-in/page.tsx:22`, `lib/auth/session.ts:23`, `chat/[[...id]]/page.tsx:45`, `matches/[id]/page.tsx:47` | http-surface §1, next-frontend §1-3 |
| Id and hash generation | `crypto.randomUUID` (chat draft id `chat/[[...id]]/page.tsx:29`; resume item ids `resume/schema.ts:179`; client editors); AI SDK `generateId` (16-char `[0-9A-Za-z]`, `api/chat/route.ts:78`, `chat/follow-up.ts:110,142`); `randomUUID` (approval ids `runs/approvals.ts:145`); sha256 (`storage/index.ts:76`); sha1 (`sources/dedupe.ts:74,90,117`); HMAC-SHA256 and randomBytes (`scripts/dev-session.ts:34-44`) | chat-agent §2, worker §17.3, auth §5 |
| Locale-dependent formatting on the server | `ranking/deep-eval.ts:163,166,176,193` (en-US, UTC ISO dates); `agents/system-prompt.ts:95` ("Today is …", **server-local TZ**); `runs/search-run.ts:270` (`toLocaleString()` with **no locale**, so it follows the process default); `format.ts plural` (`toLocaleString()` with no locale) used in `actions/companies.ts` messages; `settings/page.tsx:70-72` (en-US, UTC) | llm §24, chat-agent §9.14. The two no-locale calls are flagged here: Python must render `1,234` regardless of `LANG` |

---

## 3. Gap fills

### 3.1 DESIGN.md invariants: where each is enforced, and where the doc and the code disagree

DESIGN.md (`DESIGN.md:32-92`) says these "must survive every later change". Mapping:

| # | Invariant (short) | Enforced at | Port note |
|---|---|---|---|
| 1 | Everything intelligent runs server-side; keys and the job mirror never leave the server | All LLM calls are in `src/lib`; the settings page sends key presence only (`settings/page.tsx`, comment `:241-245`) | Django keeps keys server-side; the API must never echo keys or raw `jobs.raw` |
| 2 | Job id = `source:company_slug:external_id`; career pages `web:<slug>:<sha1(url)>` | `sources/dedupe.ts:103-119` | worker §22.13 (WHATWG URL serialization) |
| 3 | Adapters normalize; no source-specific branching outside `sources/<source>.ts` | `sources/types.ts:3-9`, `sources/index.ts:7-13` | Python package `sources/` with the same boundary |
| 4 | `description_raw` never rewritten | `sources/ingest.ts` writes only `excluded.*`; nothing else updates it | (careers-page stores cleaned text as raw: worker §17.6) |
| 5 | Boilerplate stripped only by pattern, only in the trailing 30% | `sources/clean.ts:19-184` | worker §17.1 |
| 6 | `fetched_at` is cache metadata | `ingest.ts` | — |
| 7 | Vanished postings → `is_active=false` | `ingest.ts` deactivate step (board fetches only) | worker §22.21 bug: identical reappearing postings stay inactive |
| 8 | Every per-user query takes `userId` first | `db/queries/*` | database §5 |
| 9 | Ask for a purpose, never a model; quirks only in `models.ts` | `llm/models.ts` | llm §3 |
| 10 | Every LLM call writes `llm_calls` via middleware on the model | `llm/ledger.ts`, `llm/models.ts:124-132` | llm §8 (bake-off is the deliberate exception) |
| 11 | Import boundaries | `eslint.config.mjs` | After the port this becomes a "frontend imports no server module" rule |
| 12 | Nothing heavy inside a Next request; chat capped with `isStepCount(8)` **and a timeout** | **Not fully true.** Uploads (LLM extraction), `addCompanyAction` (discovery plus a board fetch) and `refreshCompanyAction` (a board fetch, or careers-page LLM extraction) run inline, and the chat route has no timeout (http-surface §9, chat-agent §1.1). | Decide per endpoint: keep inline under ASGI with explicit timeouts, or make it a job |
| 13 | One set of resume facts: latest `resumes(base)`; `profiles.data` only mirrors it | `queries/profile.ts:72-133`. **But** `getOnboardingState` reads the resume from `profiles.data` (`profile/onboarding.ts:34`), and `get_profile_summary` counts from `profiles.data` (chat-agent §5.3 tool 1) | Both stay correct only while every base write also writes the mirror (`saveBaseResume`). The Python port must keep that dual write, or read `resumes(base)` everywhere |
| 14 | One role, one match (prefilter `DISTINCT ON roleKey`, skip roles the user already has) | `ranking/prefilter.ts:298-414` | llm §11c |
| 15 | No silent re-spend (`retryLimit: 0`), no zombie runs (boot reaper) | `queue/boss.ts:31-56`, `runs/runs.ts:225-249` | **Zombie protection covers `running` only, not `queued`.** See 3.4 |
| 16 | A search follows its request (`applyBrief`; the run's own postings only) | `ranking/brief.ts:114-125`, `queries/matches.ts:359-391` | llm §10, §23.16 |
| 17 | Work started in chat reports back in that chat | `runs/events.ts:45-74`, `chat/follow-up.ts` | chat-agent §6 |

DESIGN.md statements the code does **not** implement. Don't port them as features; they are aspirational:
- "ambiguous names become a `company_add` approval" (`DESIGN.md:251`). There is no code path. `company_add`
  exists only in the enum (`schema/enums.ts:94`) and in the `LiveApproval` type (`runs/live.ts:54`).
- "`canonical_job_id`" for cross-source duplicates (`DESIGN.md:255`). It is never written; the prefilter only
  filters `IS NULL` (`ranking/prefilter.ts:336`).
- "`call.ts` writes the `llm_calls` ledger" (`DESIGN.md:189`). Since invariant 10 it is
  `llm/ledger.ts` middleware.
- "`LLM_DEFAULT=<provider>:<model>` is required" (`DESIGN.md:182-183`). It is optional in `src/env.ts:52`;
  failure happens at first use (`ModelNotConfiguredError`, llm §3).
- `JobSourceAdapter` Phase-1 implementations "greenhouse, lever, ashby, jsearch, careers-page"
  (`DESIGN.md:175`). Only the three ATS adapters implement the interface (`sources/index.ts:7`);
  JSearch and careers-page are free functions.
- `ResumeRenderer` and `BrowserProvider` interfaces (`DESIGN.md:176,178`) do not exist in code; only
  `render_engine` (`html|latex`) is recorded (`queries/resumes.ts:136-150`). `Storage` does exist
  (`storage/index.ts:21-26`).
- "p-limit per source" (`DESIGN.md:254`). There is no p-limit dependency; the code uses `lib/pool.ts pooled()`
  and `companies/refresh.ts` pools of 6.
- `profiles.assessment` "reserved" (`DESIGN.md:243`). The column exists and is never read or written
  (database §3.3).

### 3.2 Event kinds: emitter to consumer matrix (verified against the code)

Emitters were grepped (`kind: "..."` in `src/lib`, `src/worker`, `src/app`, plus the conditional kinds
`company.discovered|company.not-found` at `worker/handlers/company.ts:35`). Consumers are:
- `announce()` toasts (`components/live/live-provider.tsx:211-247`)
- `<RefreshOn kinds>` (`home/page.tsx:61`, `matches/page.tsx:49`, `matches/[id]/page.tsx:79`,
  `activity/page.tsx:31`, `companies/page.tsx:50`, `profile/page.tsx:138`)
- `useLiveEvent` listeners (`companies/refresh-stale-button.tsx:22`, `settings/worker-panel.tsx:32`,
  `chat/chat.tsx:93-141`)
- tone in feeds (`activity/run-summary.ts:39-44`, `home/live-panels.tsx:90`)

FU marks `FOLLOW_UP_KINDS`.

| kind | level | run_id | FU | Toast | RefreshOn pages | Other listeners |
|---|---|---|---|---|---|---|
| `run.started` | info | yes | | | | feed |
| `run.completed` | info | yes | yes | success | home, matches, match, activity | chat catch-up |
| `run.failed` | error | yes | yes | error | activity | chat catch-up |
| `run.cancelled` | warn | yes | | — | activity | |
| `approval.requested` | info | yes | yes | signal toast, id `approval-<approvalId>` | home | chat catch-up; tone "signal" |
| `approval.auto` | info | yes | | — | — | |
| `approval.decided` | info | yes | | — | home, matches | |
| `approval.reopened` | warn | yes | | — | — | |
| `approval.expired` | warn | yes | | — | — | |
| `tailor.started` / `tailor.verified` | info / info\|warn | yes | | — | — | |
| `tailor.done` | info\|warn | yes | yes | success | home, match, activity | chat catch-up |
| `tailor.failed` | error | yes | yes | **none** | **none** | chat catch-up only |
| `render.done` | info | yes | | success | match, activity, profile | |
| `render.failed` | warn (inside tailor) / error (render run) | yes | | **none** | **none** | |
| `search.*`, `source.*`, `prefilter.done`, `scoring.*`, `evaluation.*`, `shortlist.*` | various | yes | | — | — | feed only |
| `company.discovered` | info | null | | plain toast | companies | |
| `company.not-found` | warn | null | | — | — | feed only |
| `company.refresh.done` | info | null | | — (the button toasts it) | companies | `RefreshStaleButton` |
| `worker.ping` | info | null | | — | — | `WorkerPanel` |
| `chat.followup` | debug | null (runId in data) | | "Foray followed up in chat: …" | — | chat reload, recent-chats reload |

Findings:
- **Tailor and render runs never emit `run.completed`/`run.failed`.** Their final kinds are `tailor.done`,
  `tailor.failed`, `render.done` and `render.failed`. `tailor.failed` and `render.failed` produce **no
  toast** and refresh no page. The user learns of the failure only through the run card's status, which
  comes from the live `runs` merge. If the Python port changes final kinds, the toast and RefreshOn
  lists must change with it. This matches CLAUDE.md's rule "a new run kind should end with run.completed
  or run.failed (or add its own final kinds there)".
- The reaper emits `run.failed` for **every** kind, tailor included (`runs/runs.ts:238-246`). An
  interrupted tailor run therefore *does* toast, but a tailor run that fails normally does not.
- `run.cancelled` from `requestCancel` is emitted only when the run had pending approvals
  (`runs/runs.ts:113-141`). Pipelines emit their own `run.cancelled` when they notice the flag. Activity's
  RefreshOn on `run.cancelled` therefore fires inconsistently; the live run merge covers the gap.
- `chat.followup` deliberately has no `run_id`, so it never re-triggers a follow-up and is never
  suppressed by `watchRuns` (chat-agent §6.2).

### 3.3 Run `stats` / `output` contract: writer to reader

UI readers:
- `run-card.tsx:27-39` (StepTrack phases)
- `run-card.tsx:56-121` (`describe`: phase, `boardsRefreshed`, `scored`, `candidates`, `evaluated`,
  `toEvaluate`; completed: `output.candidates|scored|evaluated|shortlisted|unsupported|pages`)
- `run-card.tsx:336-384` (`RunResults`: `documentId`, `matchId`, `topMatchIds`, `matchIds`)
- `activity/run-summary.ts:51-90` (`runOutcome`: `scored`, `strong`, `shortlisted`, `evaluated`,
  `unsupported`, `pages`, `ok`, `boards`; output first, then stats)
- activity `target()` (`activity/page.tsx:120-157`: `input.brief|hints|prompt|matchIds|matchId|resumeId|names|all`)

| Key / phase | Written by | Read by | Status |
|---|---|---|---|
| search phases `starting, refreshing, aggregating, filtering, scoring, awaiting_evaluation, evaluating, awaiting_shortlist, done` | `search-run.ts:146,206,246,253,288,454-455`; `evaluate-run.ts:270,414,133` | StepTrack, describe | OK |
| phase `shortlisting` | **never written** | StepTrack (`run-card.tsx:32`) | dead UI value. During gate-2 continuation `resumeFrom(run.id, approvalId)` passes no phase (`evaluate-run.ts:470`), so a running run keeps `awaiting_shortlist` and `describe()` falls through to "Working…" |
| autopilot gate 2 | `requestApproval(stats: {proposedShortlist})` with no phase (`evaluate-run.ts:412-414`) | describe | phase stays `evaluating`, so the card says "Reading N of M postings in full" while it is shortlisting (cosmetic) |
| tailor phase `starting` | **never written** (`executeTailorRun` calls `startRun` with no stats) | StepTrack (`run-card.tsx:36`) | harmless: an unknown phase maps to step 0 |
| `output.strong` (search) | **never written**. Bands (`strong`, `good`) are computed only for the `run.completed` message (`evaluate-run.ts:125-147`); `SearchRunOutput` is exactly `{candidates,newMatches,scored,autoArchived,evaluated,shortlisted,topMatchIds}` (pinned by `search-gates.test.ts`) | `runOutcome` (`run-summary.ts:59,63`) | **dead branch**: "N strong" never shows. Fix: add `strong` to output, which changes a pinned test, or drop the UI branch |
| `output.ok`, `output.boards` (company_refresh) | no `company_refresh` run is ever created (worker §25) | `runOutcome` (`run-summary.ts:82-86`) | dead UI. The same applies to `company_discovery` in activity `target()` |
| rank_deep `output {evaluated, matchIds}` | `evaluate-run.ts:628-637` | describe, runOutcome, RunResults | OK |
| tailor `output {resumeId, documentId, pages, changes, unsupported, matchId}` | `worker/handlers/resume.ts:63-72` | describe, runOutcome, RunResults | OK (`documentId` may be null when rendering failed) |
| resume_render `output {resumeId, documentId, pages}` | `handlers/resume.ts:106-109` | describe (`pages ?? 1`), RunResults | OK; no `matchId`, so no "See what changed" link |
| `stats.resumedFrom` | `evaluate-run.ts:827` | none (idempotency only) | must survive in the port |

### 3.4 Zombie `queued` runs (no map states the consequence)

Facts:
- The boot reaper fails only `status='running'` (`runs/runs.ts:225-235`). Its comment assumes
  "Queued runs are left alone (their jobs are still in the queue)" (`runs.ts:221-223`).
- `activeSearch` treats `queued` as in flight with **no age limit** (`runs.ts:40-54`).
  `startSearchAction` then refuses with "A search is already running. It will finish in a minute or two."
  (`actions/runs.ts:44-52`), and `start_search_run` returns the stale run's card with `alreadyRunning: true`
  (`agents/tools/search.ts:107-121`).
- `startMatchEvaluation` reuses any `rank_deep` run in `queued|running` with identical sorted
  `input.matchIds`, with **no age limit** (`evaluate-run.ts:530-542`). "Evaluate" for that match then
  answers "Already evaluating …" forever.
- By contrast `activeTailorRun` has a 15-minute window, "so a worker that died mid-job must not block a
  retry forever" (`tailor-run.ts:265-283`). The three dedupe guards are inconsistent.
- Search start has no try/catch around `send` in either path (`actions/runs.ts:76`,
  `agents/tools/search.ts:134`). A queue failure leaves a `queued` run that blocks searching. By contrast
  `startMatchEvaluation` (`evaluate-run.ts:552-560`), `startTailorRun` (`tailor-run.ts:337-345`) and
  `startResumeRender` (`tailor-run.ts:387-391`) fail the run when the send fails. In the UI, the throw
  from `startSearchAction` propagates out of `startTransition` (`home/live-panels.tsx:124-133`) to the
  `(app)/error.tsx` boundary.
- When can a `queued` run lose its job? A send failure (above). A handler that throws before it flips the
  status, for example a DB error in `getRun`; pg-boss marks the job failed with `retryLimit` 0. A worker
  down longer than pg-boss retention (14 days, database §8.1). **Or the queue is replaced at cutover
  without migrating `pgboss.job` rows.**
- The only escape today is the user pressing Stop: `requestCancel` accepts `queued`.

Port requirements:
1. Cutover checklist: drain `pgboss.job` (no `created`/`retry`/`active` rows), or translate them into the
   new queue. Otherwise, at cutover, fail or cancel every `queued` run whose job will not exist, with a
   `run.failed` event so chat-started runs follow up.
2. Decide whether to keep the asymmetric guards or add an age window (and/or a "queued too long"
   reaper) to `activeSearch` and the rank_deep reuse.
3. Wrap search `send` like the other starters.

### 3.5 Live-feed cursor race (bigserial visibility) — new hazard

`/api/live?since=<id>` returns `agent_events WHERE user_id=$1 AND id > since ORDER BY id LIMIT 200`, and
the client sets `cursor = last id` (`runs/live.ts:103-117`, `live-provider.tsx:103`). `agent_events.id` is
a bigserial. `nextval()` is taken at INSERT time, but the row becomes visible only at COMMIT. When two
transactions for the **same user** insert concurrently (A gets 100, B gets 101, B commits first), a poll
between the two commits returns 101 and moves the cursor past 100. Event 100 is then never delivered to
that tab. It still appears in the history (60 latest on a reload) and in `/api/runs/:id/events`.

Today the window is tiny: every `emit()` is a single autocommit INSERT (`runs/events.ts:28-40`; the only
transaction in the codebase, `saveTailoredResume`, emits nothing). Concurrent same-user emitters do exist,
though: heavy queues run up to 4 jobs at once, plus follow-ups, plus approval decisions from Next.

**Why this matters more for Django:** `ATOMIC_REQUESTS=True`, or wrapping a pipeline step in
`transaction.atomic()` together with its `emit()`, stretches the gap from microseconds to the whole
transaction. Lost toasts, missed `RefreshOn` refreshes and missed `chat.followup` reloads become
likely. Rules for the port:
- `emit()` must commit on its own (autocommit, or `transaction.atomic(durable=True)` on a separate
  connection) and must never sit inside a longer transaction.
- Alternatively, make the snapshot commit-safe: only return ids below the oldest in-flight
  `nextval`. A simple approximation is to re-query a small overlap window (`id > since - k`) and dedupe on
  the client. The client already dedupes by id only in `RunEvents`, so `LiveProvider` would need it too.

### 3.6 LLM cost-attribution quirks (affect `/activity` per-run cost and settings spend)

| Call | Context passed | Effect |
|---|---|---|
| Careers-page extraction (`sources/careers-page.ts`, purpose `extract`), from `refreshCareersPage` | `{ userId: company.addedByUserId }` (`companies/refresh.ts:131-133`), no `runId` | During user B's search, or B's "fetch now" (`refreshCompanyAction`), the cost lands on **user A, who added the company**, or on `NULL` for seed and cron companies. It never appears in the search run's cost. In a multi-tenant SaaS this bills the wrong tenant. |
| Chat turn (`ToolLoopAgent`) | `{userId}` (`api/chat/route.ts:66`) | `run_id` NULL. The discover_companies tool's nested call passes `{userId}` too. |
| Chat follow-up turn | `{userId}` (`chat/follow-up.ts:136`) | `run_id` NULL even though the turn is about a run, so per-run cost on /activity excludes follow-ups. |
| Resume extraction in `/api/uploads` | `{userId}` | no run (none exists) |
| Pipelines (deriveSearch, fast score, deep eval, tailor, verify) | `{userId, runId}` | correct |
| Bake-off | none, and not ledgered | llm §15 |

`callLLM` **replaces** any outer context with the one passed (`llm/call.ts:50`: `withLlmContext(context, …)`),
so an outer `withLlmContext` around a pipeline would not fix the careers-page case.

### 3.7 `OnboardingState` and `InterviewProgress`: exact JSON the page endpoints must return

`getOnboardingState(userId)` (`src/lib/profile/onboarding.ts:31-54`) is read by `/home`, `/matches`
(`.ready` only), `/profile`, chat `suggestionsFor`, and the chat agent's system prompt (indirectly).

```
OnboardingState = {
  hasProfile: boolean,                 // a profiles row exists
  hasResume: boolean,                  // resume parsed AND work+education+skills > 0
  resume: Resume | null,               // ResumeSchema.safeParse(profiles.data) — the MIRROR, not resumes(base)
  preferences: Preferences,            // PreferencesSchema.safeParse(profiles.preferences) or EMPTY_PREFERENCES
  progress: InterviewProgress,
  settings: Settings,                  // parseSettings(profiles.settings) (defaults when unparseable)
  ready: boolean                       // hasResume && progress.blocking.length === 0
}
InterviewProgress = {                  // profile/interview.ts:188-223
  filled: Slot[], outstanding: Slot[], blocking: Slot[],   // FULL slot objects, SLOTS order
  answered: number,                    // SLOTS.length - outstanding.length (declined counts as answered)
  total: number,                       // 8
  complete: boolean                    // outstanding.length === 0
}
Slot = { id, label, intent, fields: (keyof Preferences)[], required: boolean }  // interview.ts:99-162
```

SLOTS, in order (id → label, required):
- `target_roles` → "Target roles", required
- `seniority` → "Seniority", required
- `locations` → "Locations", required
- `remote_pref` → "Remote preference", required
- `work_auth` → "Work authorisation" (British spelling, user-visible), required
- `salary_floor` → "Salary floor"
- `start_date` → "Start date"
- `constraints` (fields `must_haves`, `dealbreakers`) → "Must-haves and dealbreakers"

`isFilled` = **any** owned field is non-null, and arrays must be non-empty (`interview.ts:179-186`; the
doc comment says "every"). The UI uses `progress.blocking[].label`, `answered`/`total`,
`resume.work|education|skills.length`, and `preferences.target_roles[0]`/`locations[0]`.
`PreferencesForm` imports `SLOTS` at runtime (`required` and labels), so the Python `SLOTS` and the
frontend copy must stay identical, or the API should serve them.

### 3.8 Server timers, sleeps, timeouts and windows (one place)

| What | Value | Where |
|---|---|---|
| Heartbeat write | every 10 s | `worker/main.ts:131` |
| Worker considered offline | `Date.now() - updated_at >= 45 s` | `system/heartbeat.ts:18,42-57` |
| Graceful drain | 30 s | `worker/main.ts:146` |
| PDF render (Playwright default timeout) | 30 s | `worker/pdf.ts:27` |
| PDF close-when-idle on signal | ≤ 25 s | `worker/pdf.ts:169-181` |
| HTTP default timeout / retries | 20 s / 3 (4 attempts) | `sources/http.ts:13-14` |
| HTTP backoff | `2^n·500 ms + rand(0..250)`; `Retry-After` seconds capped at 60 s | `sources/http.ts:32-40` |
| JSearch | 45 s / 1 retry | `sources/jsearch.ts:281-289` |
| Careers page | 15 s / 1 retry | `sources/careers-page.ts:86-90` |
| Discovery `tryFetch` / Brave | 12 s / 1 retry; Brave 12 s raw fetch | `companies/discovery.ts:121-133,155-181` |
| AI SDK transport retries | 2 retries, 2 s initial, ×2, honours `retry-after(-ms)` ≤ 60 s | llm §3c |
| Deep-eval total timeout | 240 s | `ranking/deep-eval.ts:45` |
| Follow-up agent total timeout | 90 s | `chat/follow-up.ts:141` |
| Follow-up wait for in-flight reply | ≤ 90 s, poll 1.5 s | `chat/follow-up.ts:60-61,99-102` |
| `replyInFlight` freshness | last user message < 90 s old | `chat/follow-up.ts:193-203` |
| Chat turn | **no timeout** (8-step cap only) | `api/chat/route.ts` |
| Token bucket | 300 rpm default, starts full, min sleep 25 ms | `llm/limiter.ts:28-62` |
| Approval TTL | 7 days (swept only on first live poll, chat list, or decide) | `runs/approvals.ts:29,553-583` |
| Board stale after | 2 h | `companies/refresh.ts:24-28` |
| Cron | `0 */6 * * *` UTC, missed once | `worker/main.ts:119-122` |
| Live recent-runs window | 10 min | `runs/live.ts:69` |
| Tailor dedupe window | 15 min | `runs/tailor-run.ts:277` |
| pg-boss expiry per queue | 60 s to 60 min | `queue/boss.ts:43-56` |
| pg-boss retention / deletion | 14 d / 7 d (library defaults, live) | database §8.1 |
| Session lifetime / refresh | 7 d / 1 d | auth §6 |

### 3.9 Server logging inventory (ops parity)

All logging is `console.*` with a bracketed prefix. There is no structured logger and no request log
beyond Next's own.

| Prefix | Sites |
|---|---|
| `[worker]` | `worker/main.ts:37,82,107,128,133,143,149,161`; `handlers/search.ts:18,25`; `handlers/evaluate.ts:28,37,44,59`; `handlers/ping.ts:20`; `handlers/chat.ts:10`; `handlers/resume.ts:33,39,51,95,120`; `handlers/company.ts:57,61,76` |
| `[llm]` | `llm/ledger.ts:84` (ledger write failed), `llm/structured.ts:86` (retrying once) |
| `[resume]` | `resume/extract.ts:91` (vision fallback reasons) |
| `[pdf]` | `worker/pdf.ts:91` |
| `[queue]` | `queue/send.ts:20` |
| `[migrate]` | `db/migrate.ts:12,14,20` |
| `[events]`, `[follow-up]` | `runs/events.ts:72`, `chat/follow-up.ts:164` |
| `[approvals]` | `runs/approvals.ts:464,578`, `agents/tools/approvals.ts:119`, `app/actions/approvals.ts:42` |
| `[search]`, `[evaluate]`, `[shortlist]`, `[runs]`, `[tailor]`, `[render]` | `runs/search-run.ts:504`; `runs/evaluate-run.ts:210,238,250,439,450,573,700,722`; `runs/tailor-run.ts:209,343,389` |
| `[uploads]`, `[matches]`, `[documents]` | `api/uploads/route.ts:105`, `actions/matches.ts:139`, `api/documents/[id]/route.ts:32` |

### 3.10 Retention, cleanup and spend limits: none exist

- **No pruning job anywhere.** `agent_events`, `llm_calls`, `messages`, `runs`, `approvals` and `api_usage`
  grow without bound. pg-boss prunes only its own jobs (14 d retention).
- **Sessions**: expired `session` rows are deleted only when presented (auth §6). Rows minted by
  `dev-session` accumulate (16 live rows for 1 user, database §9).
- **Storage**: never garbage-collected. Upload orphans (failed extraction, or a duplicate sha256 upload)
  stay on disk (http-surface §9). Rendered PDFs are deleted only when they are byte-identical to an
  existing document (`worker/handlers/resume.ts:180-182`). Deleting a conversation or a user cascades rows
  but never deletes files under `STORAGE_DIR/<userId>/`.
- **No app-level rate limits** on endpoints that spend money (`/api/chat`, `/api/uploads`,
  `addCompanyAction`, `evaluateMatchAction`, `tailorResumeAction`, `startSearchAction`). Better Auth's
  limiter covers only `/api/auth/*` in production. The protections that do exist:
  - the per-provider in-process token bucket (which the chat agent bypasses)
  - one active search per user
  - the rank_deep reuse
  - the 15-minute tailor dedupe
  - the JSearch monthly budget

  A Django port behind a public origin should add per-user throttles; DRF throttling can do it.

### 3.11 `revalidatePath`: the exact table (14 of 16 actions)

| Action | Paths | When |
|---|---|---|
| `decideApprovalAction` | `/home`, `/activity`, `/matches` (layout) | after any non-throwing `decideApproval`, ok or not (`actions/approvals.ts:49-51`) |
| `addCompanyAction` | `/companies` | only on success (`actions/companies.ts:83`) |
| `setStatusAction` | `/companies` | when the company exists (`:108`) |
| `refreshCompanyAction` | `/companies` | after any refresh outcome (`:130`) |
| `refreshAllAction` | — | never |
| `moveMatchStageAction` | `/matches` (layout), `/home` | on success (`actions/matches.ts:59` → helper `:147-150`) |
| `updateMatchAction` | same | on success (`:96`) |
| `evaluateMatchAction` | same | after `startMatchEvaluation` resolves, reused or new (`:119`) |
| `pingWorker` | — | never |
| `saveProfile` | `/profile` | on success (`actions/profile.ts:53`) |
| `savePreferencesAction` | `/profile`, `/home` | on success (`:107-108`) |
| `tailorResumeAction` | `/profile`, `/matches` (layout), `/home` | only when `startTailorRun` is ok (`actions/resumes.ts:36-38` → helper `:22-26`) |
| `renderBaseResumeAction` | same | only when ok (`:55`) |
| `startSearchAction` | `/home` | after `send` succeeds (`actions/runs.ts:77`) |
| `cancelRunAction` | `/home`, `/activity` | always (`:94-95`) |
| `saveSettingsAction` | `/settings` | on success (`actions/settings.ts:36`) |

Under a JSON API, every caller of these must refresh explicitly; see next-frontend §10.1 for the
components where this is load-bearing.

### 3.12 Small edge behaviours not stated elsewhere

- **Heartbeat clock skew.** `beat()` stamps `updated_at = now()` from the DB clock, but `workerStatus()`
  compares it with the **app server's** `Date.now()` (`system/heartbeat.ts:26-57`). If the Django host's
  clock runs more than about 35 s ahead of Postgres, the UI shows "worker offline". Compute
  `now() - updated_at` in SQL instead.
- **"Refresh stale boards" can wait out its patience.** `refreshAllAction` counts stale boards
  (`actions/companies.ts:154-178`) and the job recomputes them. If another refresh got there first, the
  handler logs "nothing stale" and emits nothing (`worker/handlers/company.ts:56-59`). The button then
  spins for `PATIENCE_MS` = 3 min (`refresh-stale-button.tsx`). Emitting `company.refresh.done` with
  `boards: 0` in that case would fix it.
- **Search start error page.** A queue failure in `startSearchAction` throws (no try/catch,
  `actions/runs.ts:76`). The thrown error reaches `(app)/error.tsx` and leaves a zombie `queued` run (3.4).
- **Worker-offline banner copy** says "Start it with `pnpm dev:worker`" (`components/shell/app-shell.tsx`
  WorkerLine, next-frontend §5.1). That is frontend copy to update when the worker becomes Python.
- **The `pid` in the heartbeat** is shown on Settings ("process <pid>", `worker-panel.tsx:97`). A Python
  worker should store its own pid under the same key, `value.pid`, along with `startedAt` (an ISO string)
  and `concurrency` (`system/heartbeat.ts:20-24`).

---

## 4. Cross-area contracts verified consistent

- **`LiveSnapshot` vs `LiveProvider`**: the shape `{cursor, events, runs, approvals, worker}`
  (`runs/live.ts:18-66`) matches what the provider reads (`live-provider.tsx:99-130`). `runs` are merged by
  id; `approvals` and `worker` are replaced; `cursor` is the last returned id. The UI reads every
  `WorkerStatus` field (`online`, `lastSeenAt`, `pid`: `app-shell.tsx:234-273`, `worker-panel.tsx:69-97`,
  `run-card.tsx:160`). `401` or `redirected` triggers `router.refresh()`.
- **Queue names and policies**: http-surface §6, worker §2.2, database §8.1 and tests-scripts-infra §10
  agree on all 10 queues (retry, expiry, concurrency, payloads).
- **Approval payloads**: `deep_eval_batch` and `shortlist_proposal` items carry `matchId`, which
  `ApprovalCard` requires (`approval-card.tsx:59-61`). `estimate {usd, perItemUsd, model}` matches
  `CostEstimate` (`runs/approvals.ts:58-64`).
- **FOLLOW_UP kinds and `chat.followup` data** (`{conversationId, runId, messageId?, label?, skipped?, failed?}`)
  agree across chat-agent §6, worker §12.4, database §3.4 and next-frontend §4.4.
- **Tool outputs vs `ToolPart`**: chat-agent §4.2/§5.3 lists output keys that match `tool-part.tsx:91-345`
  (`runId`, `jobs`, `queued`, `company`, `source`, `saved`, `blockingSearch`, `approvals`, `message`,
  `stage`). The one known oddity, `update_profile_preferences` returning `error` rather than `message`
  on failure, is already noted in chat-agent §4.2.

## 5. Contract mismatches between the maps (or between a map and the code)

1. **http-surface summary**: "11 of the 16 actions call revalidatePath". The code has **14 of 16**; only
   `refreshAllAction` and `pingWorker` do not (3.11). The per-action body of http-surface §4 is correct.
2. **llm §22 row x3** gives the chat agent's default reasoning effort as `low`. `chatAgent` passes **no**
   `reasoning` to `ToolLoopAgent` (`agents/chat-agent.ts:61-96`), so the provider default applies and
   `LLM_REASONING_CHAT` affects only `discover_companies`' nested `generateStructured` call.
   chat-agent §5.1 is correct.
3. **worker §2.5** names the chat tool `start_search`. Its registered name is `start_search_run`
   (`agents/chat-agent.ts:81`).
4. **next-frontend §4.5** lists the search phase `shortlisting`, the output key `strong`, and company_refresh
   output `ok`/`boards` as part of the worker contract. The worker never writes any of them (3.3).
5. **next-frontend §4.4 / worker §19** list consumers per kind but omit that `tailor.failed` and
   `render.failed` have **no** toast and no RefreshOn (3.2).
6. **DESIGN.md vs code** (3.1): `company_add` approval, `canonical_job_id`, `call.ts` writing the
   ledger, `LLM_DEFAULT` required, `JobSourceAdapter` for jsearch and careers-page, the
   `ResumeRenderer`/`BrowserProvider` interfaces, p-limit, and invariant 12's "and a timeout".
7. **DESIGN invariant 13 vs readers**: `getOnboardingState` and `get_profile_summary` read
   `profiles.data` (the mirror), not `resumes(base)` (3.1 #13, 3.7). They are consistent today only because
   `saveBaseResume` dual-writes.
8. **DESIGN invariant 15 ("no zombie runs")** holds for `running` but not for `queued` (3.4).
9. **Dedupe windows**: `activeTailorRun` has a 15-minute window, but `activeSearch` and the rank_deep
   reuse have none (3.4). This is a code inconsistency, not a map error.

---

## 6. Notes for the design step (from this pass)

- Treat 3.4 (queued zombies) and 3.5 (cursor race) as **cutover blockers**. Both get worse when the queue
  changes and when Django transaction defaults are applied.
- The event-kind matrix (3.2) and the stats/output table (3.3) form the API contract the frontend keeps.
  Freeze it as a typed schema (for example, a generated TS type) so the Python worker and the Next
  frontend cannot drift.
- Decide deliberately whether to preserve cosmetic dead paths (the `shortlisting` phase, `output.strong`)
  or fix them. `search-gates.test.ts` pins the exact search output object.

---

## 9. Appendix: per-file coverage (every tracked file except `src/test/fixtures/**` and `src/components/ui/**`)

"(by file name)" means the maps mention the file only by its bare name (inside a table or a directory
section), not by path.

| File | Maps that cover it |
|---|---|
| `.agents/skills/ai-sdk/SKILL.md` | tests-scripts-infra (by file name) |
| `.agents/skills/migrate-ai-sdk-v6-to-v7/SKILL.md` | tests-scripts-infra (by file name) |
| `.claude/launch.json` | tests-scripts-infra |
| `.claude/skills/ai-sdk` | http-surface, next-frontend, llm, chat-agent, tests-scripts-infra (by file name) |
| `.claude/skills/migrate-ai-sdk-v6-to-v7` | tests-scripts-infra (by file name) |
| `.cx.ts` | tests-scripts-infra |
| `.dockerignore` | tests-scripts-infra |
| `.env.example` | auth, llm, tests-scripts-infra |
| `.gitignore` | tests-scripts-infra |
| `.nvmrc` | tests-scripts-infra |
| `AGENTS.md` | tests-scripts-infra |
| `CLAUDE.md` | http-surface, next-frontend, auth, worker, tests-scripts-infra |
| `DESIGN.md` | http-surface, auth, database, llm, chat-agent, worker |
| `Dockerfile` | worker, tests-scripts-infra |
| `README.md` | tests-scripts-infra |
| `components.json` | tests-scripts-infra |
| `docker-compose.yml` | database, tests-scripts-infra |
| `drizzle.config.ts` | database, tests-scripts-infra |
| `drizzle/0000_calm_bloodscream.sql` | auth, database, tests-scripts-infra |
| `drizzle/0001_wealthy_human_cannonball.sql` | auth, database |
| `drizzle/0002_broken_random.sql` | database |
| `drizzle/0003_settings_and_system_state.sql` | database |
| `drizzle/meta/0000_snapshot.json` | **none** |
| `drizzle/meta/0001_snapshot.json` | **none** |
| `drizzle/meta/0002_snapshot.json` | **none** |
| `drizzle/meta/0003_snapshot.json` | database |
| `drizzle/meta/_journal.json` | database, tests-scripts-infra |
| `eslint.config.mjs` | tests-scripts-infra |
| `next.config.ts` | http-surface, tests-scripts-infra |
| `package.json` | http-surface, auth, database, llm, worker, tests-scripts-infra |
| `pnpm-lock.yaml` | tests-scripts-infra |
| `pnpm-workspace.yaml` | tests-scripts-infra |
| `postcss.config.mjs` | tests-scripts-infra |
| `public/robots.txt` | tests-scripts-infra |
| `scripts/dev-session.ts` | http-surface, auth, database, tests-scripts-infra |
| `scripts/fetch-board.ts` | worker, tests-scripts-infra |
| `scripts/llm-smoke.ts` | llm, tests-scripts-infra |
| `scripts/model-bakeoff.ts` | llm, tests-scripts-infra |
| `scripts/seed-companies.ts` | database, worker, tests-scripts-infra |
| `scripts/set-password.ts` | http-surface, auth, database, tests-scripts-infra |
| `scripts/verify-boards.ts` | worker, tests-scripts-infra |
| `skills-lock.json` | tests-scripts-infra |
| `src/app/(app)/activity/page.tsx` | next-frontend, database, llm |
| `src/app/(app)/chat/[[...id]]/page.tsx` | http-surface, next-frontend, chat-agent |
| `src/app/(app)/companies/page.tsx` | next-frontend, database |
| `src/app/(app)/error.tsx` | next-frontend |
| `src/app/(app)/home/page.tsx` | next-frontend, auth, database |
| `src/app/(app)/layout.tsx` | http-surface, next-frontend, auth |
| `src/app/(app)/loading.tsx` | next-frontend |
| `src/app/(app)/matches/[id]/page.tsx` | next-frontend, database |
| `src/app/(app)/matches/page.tsx` | next-frontend |
| `src/app/(app)/not-found.tsx` | next-frontend |
| `src/app/(app)/onboarding/page.tsx` | next-frontend |
| `src/app/(app)/profile/page.tsx` | next-frontend |
| `src/app/(app)/settings/page.tsx` | next-frontend, auth, llm |
| `src/app/(auth)/sign-in/page.tsx` | http-surface, next-frontend, auth |
| `src/app/actions/approvals.ts` | http-surface, next-frontend, worker |
| `src/app/actions/companies.ts` | http-surface, next-frontend, database, worker, tests-scripts-infra |
| `src/app/actions/matches.ts` | http-surface, next-frontend, database, llm, worker |
| `src/app/actions/ping.ts` | http-surface, next-frontend, worker |
| `src/app/actions/profile.ts` | http-surface, next-frontend, database, llm, tests-scripts-infra |
| `src/app/actions/resumes.ts` | http-surface, next-frontend, worker |
| `src/app/actions/runs.ts` | http-surface, next-frontend, database, worker, tests-scripts-infra |
| `src/app/actions/settings.ts` | http-surface, next-frontend |
| `src/app/api/auth/[...all]/route.ts` | http-surface, auth |
| `src/app/api/chat/route.ts` | http-surface, next-frontend, auth, database, llm, chat-agent, tests-scripts-infra |
| `src/app/api/conversations/[id]/route.ts` | http-surface, next-frontend, database, chat-agent |
| `src/app/api/conversations/route.ts` | http-surface, database, chat-agent |
| `src/app/api/documents/[id]/route.ts` | http-surface, next-frontend, database, chat-agent |
| `src/app/api/live/route.ts` | http-surface, next-frontend, worker |
| `src/app/api/matches/route.ts` | http-surface, next-frontend |
| `src/app/api/resumes/[id]/preview/route.ts` | http-surface, next-frontend, tests-scripts-infra |
| `src/app/api/runs/[id]/events/route.ts` | http-surface, next-frontend, database |
| `src/app/api/runs/[id]/route.ts` | http-surface, next-frontend, database, chat-agent |
| `src/app/api/uploads/route.ts` | http-surface, database, llm |
| `src/app/globals.css` | tests-scripts-infra |
| `src/app/icon.svg` | **none** |
| `src/app/layout.tsx` | next-frontend |
| `src/app/page.tsx` | http-surface, next-frontend, auth |
| `src/components/activity/activity-feed.tsx` | next-frontend (by file name) |
| `src/components/activity/activity-view.tsx` | next-frontend (by file name) |
| `src/components/activity/run-list.tsx` | http-surface |
| `src/components/activity/run-summary.ts` | next-frontend, database |
| `src/components/ai-elements/code-block.tsx` | next-frontend |
| `src/components/ai-elements/conversation.tsx` | next-frontend (by file name) |
| `src/components/ai-elements/message.tsx` | next-frontend (by file name) |
| `src/components/auth/sign-in-form.tsx` | http-surface, next-frontend, auth |
| `src/components/chat/chat.tsx` | http-surface, chat-agent |
| `src/components/chat/tool-part.tsx` | chat-agent |
| `src/components/companies/add-company.tsx` | http-surface |
| `src/components/companies/company-row.ts` | http-surface, next-frontend |
| `src/components/companies/company-table.tsx` | http-surface |
| `src/components/companies/refresh-stale-button.tsx` | http-surface |
| `src/components/foray/primitives.tsx` | next-frontend |
| `src/components/home/ask-box.tsx` | chat-agent |
| `src/components/home/live-panels.tsx` | http-surface |
| `src/components/home/onboarding-chat.tsx` | chat-agent |
| `src/components/live/live-provider.tsx` | http-surface, next-frontend, chat-agent |
| `src/components/live/refresh-on.tsx` | next-frontend |
| `src/components/matches/board.tsx` | http-surface |
| `src/components/matches/evaluation.tsx` | next-frontend (by file name) |
| `src/components/matches/match-controls.tsx` | http-surface |
| `src/components/matches/match-line.tsx` | next-frontend (by file name) |
| `src/components/matches/match-table.tsx` | http-surface, next-frontend (by file name) |
| `src/components/matches/posting.tsx` | next-frontend (by file name) |
| `src/components/matches/tailor-panel.tsx` | http-surface |
| `src/components/profile/bullet-list.tsx` | next-frontend (by file name) |
| `src/components/profile/field.tsx` | next-frontend (by file name) |
| `src/components/profile/form-controls.tsx` | next-frontend (by file name) |
| `src/components/profile/page-nav.tsx` | next-frontend (by file name) |
| `src/components/profile/preferences-form.tsx` | http-surface |
| `src/components/profile/profile-editor.tsx` | http-surface |
| `src/components/profile/profile-header.tsx` | http-surface |
| `src/components/profile/resume-dropzone.tsx` | http-surface |
| `src/components/profile/skills-editor.tsx` | next-frontend (by file name) |
| `src/components/runs/approval-card.tsx` | http-surface |
| `src/components/runs/run-card.tsx` | http-surface |
| `src/components/settings/appearance-form.tsx` | next-frontend |
| `src/components/settings/behaviour-form.tsx` | http-surface |
| `src/components/settings/sign-out-button.tsx` | http-surface, auth |
| `src/components/settings/spend.tsx` | next-frontend (by file name) |
| `src/components/settings/worker-panel.tsx` | http-surface |
| `src/components/shell/app-shell.tsx` | next-frontend (by file name) |
| `src/components/shell/recent-chats.tsx` | http-surface, chat-agent |
| `src/components/shell/theme-sync.tsx` | next-frontend |
| `src/components/shell/toaster.tsx` | next-frontend |
| `src/components/shell/user-menu.tsx` | http-surface, next-frontend, auth |
| `src/env.ts` | http-surface, next-frontend, auth, database, llm, worker, tests-scripts-infra |
| `src/lib/agents/chat-agent.ts` | http-surface, next-frontend, database, chat-agent, worker |
| `src/lib/agents/system-prompt.ts` | chat-agent, tests-scripts-infra |
| `src/lib/agents/tools/approvals.ts` | chat-agent, worker |
| `src/lib/agents/tools/companies.ts` | database, llm, chat-agent, worker |
| `src/lib/agents/tools/get-profile-summary.ts` | database, chat-agent |
| `src/lib/agents/tools/matches.ts` | llm, chat-agent |
| `src/lib/agents/tools/resume.ts` | chat-agent |
| `src/lib/agents/tools/search.ts` | next-frontend, database, chat-agent, worker |
| `src/lib/agents/tools/tailor.ts` | chat-agent |
| `src/lib/agents/tools/update-profile-preferences.ts` | database, chat-agent |
| `src/lib/auth/client.ts` | http-surface, next-frontend, auth, tests-scripts-infra |
| `src/lib/auth/index.ts` | http-surface, next-frontend, auth, database, tests-scripts-infra |
| `src/lib/auth/session.ts` | http-surface, next-frontend, auth, chat-agent |
| `src/lib/chat/follow-up-kinds.ts` | http-surface, next-frontend, database, chat-agent, worker, tests-scripts-infra |
| `src/lib/chat/follow-up.ts` | auth, database, llm, chat-agent, worker, tests-scripts-infra |
| `src/lib/companies/discovery.ts` | http-surface, next-frontend, database, chat-agent, worker, tests-scripts-infra |
| `src/lib/companies/refresh.ts` | http-surface, database, worker |
| `src/lib/companies/seed.json` | database, worker, tests-scripts-infra |
| `src/lib/companies/verify.ts` | worker, tests-scripts-infra |
| `src/lib/db/index.ts` | database, worker, tests-scripts-infra |
| `src/lib/db/migrate.ts` | database, tests-scripts-infra |
| `src/lib/db/queries/companies.ts` | http-surface, database, worker, tests-scripts-infra |
| `src/lib/db/queries/conversations.ts` | next-frontend, database, chat-agent |
| `src/lib/db/queries/matches.ts` | http-surface, next-frontend, database, llm, chat-agent, worker, tests-scripts-infra |
| `src/lib/db/queries/profile.ts` | http-surface, next-frontend, database, chat-agent, worker |
| `src/lib/db/queries/resumes.ts` | http-surface, next-frontend, database, worker |
| `src/lib/db/queries/spend.ts` | next-frontend, database, llm |
| `src/lib/db/queries/usage.ts` | database, worker |
| `src/lib/db/schema/auth.ts` | http-surface, auth, database |
| `src/lib/db/schema/chat.ts` | database, llm, chat-agent |
| `src/lib/db/schema/enums.ts` | database, llm, worker |
| `src/lib/db/schema/index.ts` | database, tests-scripts-infra |
| `src/lib/db/schema/jobs.ts` | next-frontend, database, worker |
| `src/lib/db/schema/profile.ts` | database, worker |
| `src/lib/db/schema/runs.ts` | http-surface, database, worker |
| `src/lib/db/schema/usage.ts` | database, worker |
| `src/lib/format.ts` | http-surface, next-frontend, tests-scripts-infra |
| `src/lib/llm/call.ts` | llm, tests-scripts-infra |
| `src/lib/llm/context.ts` | http-surface, database, llm, chat-agent, tests-scripts-infra |
| `src/lib/llm/ledger.ts` | database, tests-scripts-infra |
| `src/lib/llm/limiter.ts` | llm |
| `src/lib/llm/models.ts` | next-frontend, llm, chat-agent, tests-scripts-infra |
| `src/lib/llm/pricing.ts` | next-frontend, llm, tests-scripts-infra |
| `src/lib/llm/registry.ts` | next-frontend, llm, chat-agent, tests-scripts-infra |
| `src/lib/llm/structured.ts` | llm, chat-agent |
| `src/lib/load-dotenv.ts` | database, tests-scripts-infra |
| `src/lib/pool.ts` | database, llm, worker, tests-scripts-infra |
| `src/lib/profile/interview.ts` | http-surface, next-frontend, database, llm, chat-agent, tests-scripts-infra |
| `src/lib/profile/onboarding.ts` | next-frontend, llm, chat-agent |
| `src/lib/profile/settings.ts` | http-surface, next-frontend, database, llm, chat-agent |
| `src/lib/queue/boss.ts` | http-surface, database, chat-agent, worker, tests-scripts-infra |
| `src/lib/queue/queues.ts` | http-surface, database, chat-agent, worker, tests-scripts-infra |
| `src/lib/queue/send.ts` | http-surface, database, worker |
| `src/lib/ranking/bakeoff-fixture.ts` | database, llm, tests-scripts-infra |
| `src/lib/ranking/brief.ts` | http-surface, next-frontend, database, llm, chat-agent, tests-scripts-infra |
| `src/lib/ranking/deep-eval.ts` | http-surface, next-frontend, database, llm, chat-agent, tests-scripts-infra |
| `src/lib/ranking/fast-score.ts` | database, llm |
| `src/lib/ranking/prefilter.ts` | database, llm, worker, tests-scripts-infra |
| `src/lib/ranking/schemas.ts` | database, llm, tests-scripts-infra |
| `src/lib/resume/edits.ts` | http-surface, llm, tests-scripts-infra |
| `src/lib/resume/extract.ts` | http-surface, llm |
| `src/lib/resume/parse/docx.ts` | llm |
| `src/lib/resume/parse/garble.ts` | http-surface, llm, tests-scripts-infra |
| `src/lib/resume/parse/pdf.ts` | llm |
| `src/lib/resume/render/template.ts` | http-surface, llm, tests-scripts-infra |
| `src/lib/resume/schema.ts` | http-surface, next-frontend, database, llm, tests-scripts-infra |
| `src/lib/resume/tailor.ts` | database, llm, worker, tests-scripts-infra |
| `src/lib/resume/text.ts` | chat-agent, tests-scripts-infra |
| `src/lib/resume/verify.ts` | database, llm, worker, tests-scripts-infra |
| `src/lib/runs/approvals.ts` | http-surface, database, chat-agent, worker, tests-scripts-infra |
| `src/lib/runs/evaluate-run.ts` | http-surface, database, chat-agent, worker, tests-scripts-infra |
| `src/lib/runs/events.ts` | http-surface, database, chat-agent, worker, tests-scripts-infra |
| `src/lib/runs/live.ts` | http-surface, next-frontend, database, worker |
| `src/lib/runs/runs.ts` | http-surface, next-frontend, database, chat-agent, worker |
| `src/lib/runs/search-run.ts` | http-surface, database, chat-agent, worker |
| `src/lib/runs/tailor-run.ts` | http-surface, llm, chat-agent, worker |
| `src/lib/sources/ashby.ts` | worker |
| `src/lib/sources/careers-page.ts` | llm, worker |
| `src/lib/sources/clean.ts` | llm, worker, tests-scripts-infra |
| `src/lib/sources/dedupe.ts` | database, worker, tests-scripts-infra |
| `src/lib/sources/greenhouse.ts` | worker |
| `src/lib/sources/http.ts` | worker |
| `src/lib/sources/index.ts` | worker |
| `src/lib/sources/ingest.ts` | database, worker |
| `src/lib/sources/jsearch.ts` | next-frontend, database, worker, tests-scripts-infra |
| `src/lib/sources/lever.ts` | worker |
| `src/lib/sources/normalize.ts` | worker, tests-scripts-infra |
| `src/lib/sources/types.ts` | database, worker, tests-scripts-infra |
| `src/lib/storage/index.ts` | http-surface, database, worker, tests-scripts-infra |
| `src/lib/system/heartbeat.ts` | http-surface, next-frontend, database, worker |
| `src/lib/theme.ts` | next-frontend, tests-scripts-infra |
| `src/lib/utils.ts` | next-frontend, tests-scripts-infra |
| `src/proxy.ts` | http-surface, next-frontend, auth, chat-agent, tests-scripts-infra |
| `src/test/setup.ts` | tests-scripts-infra |
| `src/test/unit/adapters.test.ts` | worker, tests-scripts-infra (by file name) |
| `src/test/unit/approvals.test.ts` | database |
| `src/test/unit/brief.test.ts` | tests-scripts-infra (by file name) |
| `src/test/unit/careers-page.test.ts` | worker, tests-scripts-infra (by file name) |
| `src/test/unit/clean.test.ts` | worker, tests-scripts-infra (by file name) |
| `src/test/unit/deep-eval.test.ts` | tests-scripts-infra (by file name) |
| `src/test/unit/discovery.test.ts` | worker, tests-scripts-infra (by file name) |
| `src/test/unit/follow-up-db.test.ts` | auth |
| `src/test/unit/follow-up.test.ts` | chat-agent |
| `src/test/unit/format.test.ts` | next-frontend |
| `src/test/unit/interview.test.ts` | llm, tests-scripts-infra (by file name) |
| `src/test/unit/jsearch.test.ts` | worker, tests-scripts-infra (by file name) |
| `src/test/unit/normalize.test.ts` | worker, tests-scripts-infra (by file name) |
| `src/test/unit/prefilter.test.ts` | tests-scripts-infra (by file name) |
| `src/test/unit/pricing.test.ts` | llm |
| `src/test/unit/resume-edits.test.ts` | tests-scripts-infra (by file name) |
| `src/test/unit/resume-template.test.ts` | tests-scripts-infra (by file name) |
| `src/test/unit/search-gates.test.ts` | database, worker, tests-scripts-infra (by file name) |
| `src/test/unit/tailor.test.ts` | tests-scripts-infra (by file name) |
| `src/test/unit/token-accounting.test.ts` | llm |
| `src/test/unit/verify.test.ts` | tests-scripts-infra (by file name) |
| `src/worker/handlers/chat.ts` | chat-agent, worker |
| `src/worker/handlers/company.ts` | database, worker |
| `src/worker/handlers/evaluate.ts` | worker |
| `src/worker/handlers/ping.ts` | worker |
| `src/worker/handlers/resume.ts` | database, llm, worker |
| `src/worker/handlers/search.ts` | worker |
| `src/worker/main.ts` | database, chat-agent, worker, tests-scripts-infra |
| `src/worker/pdf.ts` | worker, tests-scripts-infra |
| `tsconfig.json` | tests-scripts-infra |
| `vitest.config.ts` | tests-scripts-infra |
