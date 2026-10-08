# Area map: worker (background processing)

Repo root: `/Users/haydenbarolette/Projects/JobSearchAgent/.claude/worktrees/middleware-django-migration-389b1a`
All paths below are relative to it. `file:line` cites the current TypeScript.

Scope: `src/worker/**`, `src/lib/queue/**`, `src/lib/runs/**`, `src/lib/sources/**`,
`src/lib/companies/**`, `src/lib/system/heartbeat.ts`, `src/lib/storage/**`, plus the
DB queries those call (`src/lib/db/queries/{companies,usage,matches,resumes,profile}.ts`),
`src/lib/pool.ts`, `src/lib/chat/follow-up*.ts` (worker-executed), and scripts
`seed-companies.ts`, `fetch-board.ts`, `verify-boards.ts`.

Out of scope but called (interfaces only, see the ranking / resume / llm / chat maps):
`lib/ranking/{prefilter,fast-score,deep-eval,brief}.ts`, `lib/resume/{tailor,verify,render/template}.ts`,
`lib/llm/*`, `lib/agents/chat-agent.ts`, `lib/profile/{interview,settings}.ts`.

---

## 0. File inventory

| File | Lines | Role |
|---|---|---|
| src/worker/main.ts | 163 | worker process entry: boot, reaper, queue registration, cron, heartbeat, shutdown |
| src/worker/pdf.ts | 215 | the only Playwright importer; HTML to Letter PDF, page count |
| src/worker/handlers/ping.ts | 22 | `ping` |
| src/worker/handlers/company.ts | 93 | `company.discover`, `company.refresh` |
| src/worker/handlers/search.ts | 28 | `search` |
| src/worker/handlers/evaluate.ts | 62 | `search.evaluate`, `search.shortlist`, `match.evaluate` |
| src/worker/handlers/resume.ts | 208 | `resume.tailor`, `resume.render`, `renderStoredResume` |
| src/worker/handlers/chat.ts | 12 | `chat.follow_up` |
| src/lib/queue/boss.ts | 70 | pg-boss construction + per-queue policy |
| src/lib/queue/queues.ts | 110 | queue names + payload types |
| src/lib/queue/send.ts | 40 | app-side send-only handle |
| src/lib/runs/runs.ts | 252 | run lifecycle primitives, reaper, cancel |
| src/lib/runs/events.ts | 74 | `emit()` + chat follow-up hook |
| src/lib/runs/approvals.ts | 629 | approval gates / continuation pattern |
| src/lib/runs/live.ts | 172 | `/api/live` snapshot |
| src/lib/runs/search-run.ts | 667 | search run up to gate 1 |
| src/lib/runs/evaluate-run.ts | 868 | gate 1 + gate 2 continuations, on-demand evaluation |
| src/lib/runs/tailor-run.ts | 399 | tailor run, start helpers for tailor/render |
| src/lib/system/heartbeat.ts | 57 | worker heartbeat in `system_state` |
| src/lib/storage/index.ts | 77 | local filesystem storage |
| src/lib/sources/types.ts | 90 | NormalizedJob etc. |
| src/lib/sources/index.ts | 16 | BOARD_ADAPTERS registry |
| src/lib/sources/http.ts | 111 | polite fetch w/ retry/backoff |
| src/lib/sources/greenhouse.ts | 153 | Greenhouse adapter |
| src/lib/sources/lever.ts | 189 | Lever adapter |
| src/lib/sources/ashby.ts | 185 | Ashby adapter |
| src/lib/sources/jsearch.ts | 336 | JSearch (RapidAPI) aggregator |
| src/lib/sources/careers-page.ts | 251 | LLM careers-page extractor |
| src/lib/sources/clean.ts | 218 | HTML to text, tail boilerplate removal |
| src/lib/sources/normalize.ts | 290 | location / remote / salary parsing |
| src/lib/sources/dedupe.ts | 119 | dedupe key, content hash, job ids |
| src/lib/sources/ingest.ts | 164 | upsert into `jobs` mirror |
| src/lib/companies/discovery.ts | 445 | name/URL to verified board |
| src/lib/companies/verify.ts | 97 | board verification, slug guesses |
| src/lib/companies/refresh.ts | 173 | refresh one/many companies |
| src/lib/companies/seed.json | 2403 | 225 seed companies |
| src/lib/chat/follow-up.ts | 409 | worker-run chat follow-up |
| src/lib/chat/follow-up-kinds.ts | 22 | FOLLOW_UP_KINDS, FOLLOW_UP_EVENT |
| src/lib/pool.ts | 27 | `pooled()` bounded concurrency |

---

## 1. Worker process (src/worker/main.ts)

### 1.1 Boot sequence
1. `import "@/lib/load-dotenv"` (main.ts:1) — `process.loadEnvFile()` best-effort (load-dotenv.ts:10-14). No .env is fine.
2. `const boss = createBoss({ role: "worker" })` (main.ts:35); `boss.on("error", console.error)` (main.ts:37).
3. `main()` (main.ts:99-134):
   1. `await boss.start()` — pg-boss migrates/creates schema `pgboss` (worker role only).
   2. `await ensureQueues(boss)` (boss.ts:64-70) — create + update every queue with its policy.
   3. if `stopping` return (a signal arrived during boot; main.ts:93-97,102).
   4. **Reaper**: `reapInterruptedRuns()` (runs.ts:225-249). Logs `[worker] marked N interrupted run(s) failed`. Must run *before* taking work.
   5. if `stopping` return (main.ts:108).
   6. `register(boss)` (main.ts:67-88): for each queue `worker.work(queue, { batchSize: 1, localConcurrency: concurrency }, handler)`. The wrapper catches, logs `[worker] <queue> failed:` and **rethrows** so pg-boss marks the job failed (and retries per policy).
   7. Cron: `boss.schedule("company.refresh", "0 */6 * * *", {}, { key: "company-refresh-cron", missed: "once" })` (main.ts:119-122). Every 6 h at minute 0 (server TZ = UTC in pg-boss), at most one catch-up run for missed windows. Payload `{}` (but see 5.2: pg-boss hands `data` as null for cron jobs; handler defends with `job.data ?? {}`).
   8. Heartbeat: `beat({ pid, startedAt, concurrency: heavy })` immediately, then `setInterval(pulse, 10_000)` (main.ts:126-131). Errors logged, never fatal.
   9. Log `[worker] ready (pid X, concurrency N)`.
4. `main().catch` (main.ts:159-163): if not stopping, log and `process.exit(1)`.

### 1.2 Concurrency constants
- `heavy = Math.max(1, Math.min(env.WORKER_CONCURRENCY, 4))` (main.ts:48). `WORKER_CONCURRENCY` default 4 (env.ts:82). So heavy is in [1,4].
- Per-queue `localConcurrency` (main.ts:52-65):

| Queue | Handler | Concurrency |
|---|---|---|
| `ping` | handlePing | 1 |
| `company.discover` | handleCompanyDiscover | 2 |
| `company.refresh` | handleCompanyRefresh | 1 ("a refresh already fans out across boards internally, and two overlapping refreshes would double the load on free public APIs", main.ts:55-57) |
| `search` | handleSearch | heavy |
| `search.evaluate` | handleSearchEvaluate | heavy |
| `search.shortlist` | handleSearchShortlist | heavy |
| `match.evaluate` | handleMatchEvaluate | heavy |
| `resume.tailor` | handleResumeTailor | heavy |
| `resume.render` | handleResumeRender | 2 |
| `chat.follow_up` | handleChatFollowUp | 2 |

"Concurrency is per queue, so a long search never blocks a tailoring job the user is waiting on. The LLM work inside each is still bounded by the per-provider limiter" (main.ts:41-47).

### 1.3 Shutdown (main.ts:140-157)
- On SIGINT / SIGTERM: if already stopping return; `stopping = true`; log `[worker] <SIG> — draining`; `clearInterval(heartbeat)`; `boss.stop({ graceful: true, close: true, timeout: 30_000 })`; `pool.end()`; finally `process.exit(0)`.
- Rationale: "Stop fetching new jobs, let in-flight ones finish, then close the pool. A hard exit would leave jobs marked active until pg-boss's expiry reaped them." (main.ts:136-139).
- pdf.ts installs additional `process.once(signal)` hooks that close the browser when idle (see 15).

### 1.4 Deployment
- Dockerfile target `worker`: `mcr.microsoft.com/playwright:v1.63.0-noble` image, `CMD node_modules/.bin/tsx src/worker/main.ts` (Dockerfile). Shares the `storage-data:/app/data` volume with `web` (docker-compose: worker/web volumes). A single worker is assumed (reaper relies on it, runs.ts:218-224).
- Dev: `pnpm dev:worker` = `tsx watch src/worker/main.ts` — restarts on every save; reaper fails the in-flight run.

---

## 2. Queue layer (src/lib/queue)

### 2.1 pg-boss construction — `createBoss({ role })` (boss.ts:12-28)
```
new PgBoss({
  connectionString: env.DATABASE_URL,
  schema: "pgboss",
  migrate:   role === "worker",   // worker owns DDL; avoid two processes racing DDL
  supervise: role === "worker",   // expiry / maintenance
  schedule:  role === "worker",   // cron
  useListenNotify: role === "worker",  // wake on send, "Search started" followed by activity within a beat
})
```
pg-boss 12.33.2 (`node_modules/pg-boss/package.json`). Same Postgres as app tables ("a job and the rows it writes can share a transaction", boss.ts:9-10 — note: no code actually sends inside a transaction today).

### 2.2 Per-queue policy — `POLICIES` (boss.ts:43-56)
`MINUTE = 60`. All queues are pg-boss default policy (`standard`), `notify: true`.

| Queue name (wire string) | retryLimit | expireInSeconds | Payload type (queues.ts) |
|---|---|---|---|
| `ping` | 0 | 60 | `{ userId: string; note?: string }` (39-42) |
| `company.discover` | 1 | 900 | `{ userId: string; names: string[]; runId?: string \| null }` (44-49) |
| `company.refresh` | 0 | 3600 | `{ userId?: string \| null; runId?: string \| null; all?: boolean }` (51-57); cron sends `{}`/null |
| `search` | 0 | 3600 | `{ userId: string; runId: string }` (59-62) |
| `search.evaluate` | 0 | 3600 | `SearchContinuationPayload = { userId; runId; approvalId }` (65-69) |
| `search.shortlist` | 0 | 300 | `SearchContinuationPayload` |
| `match.evaluate` | 0 | 1800 | `{ userId; runId; matchIds: string[] }` (71-76) |
| `resume.tailor` | 0 | 1800 | `{ userId; runId; matchId }` (78-83) |
| `resume.render` | 1 | 600 | `{ userId; runId; resumeId }` (85-90) |
| `chat.follow_up` | 0 | 300 | `{ userId; runId; eventId: number }` (92-97) |

Why retryLimit 0: "Zero for anything that spends money on a model: those handlers record their own failure on the run, and a silent automatic re-run would pay for the same work twice. pg-boss defaults to 2." (boss.ts:31-35). chat.follow_up: "A follow-up that fails is skipped, not retried: by the time a retry ran, the news it would report could be stale." (boss.ts:53-54). DESIGN.md invariant 15.

### 2.3 `ensureQueues(boss)` (boss.ts:64-70)
For every queue: `createQueue(name, { ...policy, notify: true })` then `updateQueue(name, { ...policy, notify: true })` — "Creating one that already exists is a no-op, so the policy is applied with an update as well — otherwise a policy change here would never reach a queue created by an earlier version." Called by both worker (boot) and sender (first connect).

### 2.4 Sender — `src/lib/queue/send.ts`
- `sender()` caches `connect()` promise on `globalThis.__foraySender` (send.ts:14-30) to survive Next hot reload. `connect()` = `createBoss({ role: "sender" })`, error listener, `start()`, `ensureQueues`.
- `send(queue, payload, options?) -> Promise<string|null>` (send.ts:33-40) → `boss.send(queue, payload, options ?? {})`; returns pg-boss job id. (Comment says "runs record for cancellation" but `runs.pgboss_job_id` is never written anywhere.)
- Only option ever used: `{ singletonKey: "follow-up-<eventId>" }` (events.ts:66-70). **Under the `standard` policy pg-boss 12 does not enforce singletonKey uniqueness** (unique indexes job_i1..i6 only apply to short/singleton/stately/exclusive policies or when `singleton_on` is set; node_modules/pg-boss/dist/plans.js:805-847). So follow-up dedupe actually relies on `alreadyFollowedUp()` in follow-up.ts:205-218.

### 2.5 Producers of each queue (who enqueues)
| Queue | Producer(s) |
|---|---|
| ping | `pingWorker()` server action src/app/actions/ping.ts:8-15 (note "The worker answered a test job from Settings.") |
| company.discover | chat tool `discover_companies` src/lib/agents/tools/companies.ts:103 (`{ userId, names }`, no runId) |
| company.refresh | cron (main.ts:119); `refreshAllAction` src/app/actions/companies.ts:169 (`{ userId, all: false }`) |
| search | `startSearchAction` src/app/actions/runs.ts:76; chat tool `start_search` src/lib/agents/tools/search.ts:134 |
| search.evaluate | only as an approval continuation (search-run.ts:448-451) |
| search.shortlist | only as an approval continuation (evaluate-run.ts:407-410) |
| match.evaluate | `startMatchEvaluation` evaluate-run.ts:553 (called from actions/matches.ts:116 and chat tool matches.ts:98) |
| resume.tailor | `startTailorRun` tailor-run.ts:337 (actions/resumes.ts:35, chat tool tailor.ts:25) |
| resume.render | `startResumeRender` tailor-run.ts:387 (actions/resumes.ts:54) |
| chat.follow_up | `emit()` hook, events.ts:57-74 |

---

## 3. Tables touched by this area (shapes; DDL lives in src/lib/db/schema)

### 3.1 `runs` (schema/runs.ts:30-71)
`id uuid pk default random`, `user_id text not null fk user cascade`, `kind run_kind not null`, `parent_run_id uuid` (never written), `mode run_mode not null default 'approve_each'`, `status run_status not null default 'queued'`, `input jsonb not null default {}`, `stats jsonb not null default {}`, `output jsonb`, `error text`, `conversation_id uuid`, `pgboss_job_id text` (never written), `started_at`, `finished_at`, `created_at not null default now()`, `updated_at not null default now()` (all timestamptz). Indexes: (user_id, created_at desc), (user_id, status), (parent_run_id).
Enums (schema/enums.ts): `run_kind` = search | rank_deep | tailor | resume_render | company_discovery | company_refresh | profile_import (last three never created by code; only UI labels). `run_mode` = approve_each | autopilot. `run_status` = queued | running | awaiting_approval | completed | failed | cancelled.

### 3.2 `agent_events` (schema/runs.ts:77-103)
`id bigserial pk` (monotonic; `/api/live?since=` cursor), `user_id text not null fk cascade`, `run_id uuid fk runs cascade null`, `level event_level not null default 'info'` (debug|info|warn|error), `kind text not null`, `message text not null`, `data jsonb`, `approval_id uuid`, `created_at`. Indexes (user_id, id), (run_id, id).

### 3.3 `approvals` (schema/runs.ts:113-147)
`id uuid pk`, `user_id`, `run_id uuid not null fk runs cascade`, `kind approval_kind` (deep_eval_batch | shortlist_proposal | tailor | company_add — only first two raised by code), `title text not null`, `payload jsonb not null default {}`, `status approval_status not null default 'pending'` (pending|approved|rejected|expired|auto_approved), `decision jsonb`, `decided_via decided_via` (ui|chat|autopilot), `decided_at`, `continuation jsonb not null` (`{queue, payload}`), `expires_at`, `created_at`. Indexes (user_id,status), (run_id).

### 3.4 `system_state` (schema/usage.ts:42-48): `key text pk`, `value jsonb not null default {}`, `updated_at`.
### 3.5 `api_usage` (schema/usage.ts:19-33): `id text pk` (`provider:YYYY-MM`), `provider`, `period` (UTC `YYYY-MM`), `requests int not null default 0`, `updated_at`. Index (provider, period).
### 3.6 `companies`, `jobs`, `job_matches` (schema/jobs.ts) — see 17.10 for the columns this area writes. `companies.slug` unique; partial unique index `(source_type, ats_slug) where ats_slug is not null` (jobs.ts:74-76). `jobs.id` text pk; GIN FTS index over title+description_text; partial index (posted_at desc) where is_active. `job_matches` unique (user_id, job_id).
### 3.7 `documents` (schema/profile.ts:71-98): unique (user_id, sha256). `resumes` (profile.ts:100-146): `rendered_document_id`, `render_engine` (html|latex), `job_match_id`, `version`, `data`, `changes`, `verification`.

---

## 4. Run lifecycle primitives (src/lib/runs/runs.ts)

`UUID_RE = /^[0-9a-f]{8}-…-[0-9a-f]{12}$/i` (runs.ts:251-252). Any id failing it is treated as "not found" instead of a DB error.

| Function | Signature | SQL / behaviour |
|---|---|---|
| `createRun` (19-22) | `(input: NewRun) -> Run` | `INSERT INTO runs … RETURNING *` |
| `getRun` (24-33) | `(userId, id) -> Run\|null` | UUID check; `SELECT * FROM runs WHERE id=$id AND user_id=$user LIMIT 1` |
| `activeSearch` (40-54) | `(userId) -> Run\|null` | newest `kind='search' AND status IN (queued,running,awaiting_approval)`. Callers refuse a 2nd concurrent search ("would score the same postings twice and race each other's approvals") |
| `listRuns` (56-63) | `(userId, limit=20)` | newest first |
| `startRun` (65-70) | `(id)` | **unconditional** `SET status='running', started_at=now, updated_at=now` |
| `mergeStats` (73-81) | `(id, patch)` | `SET stats = stats \|\| $patch::jsonb, updated_at=now` — shallow merge "so concurrent steps do not clobber each other" |
| `finishRun` (83-100) | `(id, {status:'completed',output?} \| {status:'failed',error} \| {status:'cancelled'})` | **unconditional** `SET status, output=(output??null), error=(error??null), finished_at=now, updated_at=now` |
| `requestCancel` (113-141) | `(userId, id) -> Run\|null` | `UPDATE runs SET status='cancelled', finished_at=now, updated_at=now WHERE id AND user_id AND status IN ('queued','running','awaiting_approval') RETURNING *`; null if nothing updated. Then `expireRunApprovals(run.id)`; if >0 expired emit `run.cancelled` warn "Cancelled while waiting for your answer. Everything found so far was kept." (paused run has no handler alive to report it) |
| `setRunStatusIf` (152-173) | `(id, from[], to, stats?) -> bool` | `UPDATE runs SET status=$to, [started_at=coalesce(started_at, now()) if to='running'], [stats=stats\|\|$stats], updated_at=now WHERE id AND status IN from RETURNING id` |
| `finishActiveRun` (180-202) | `(id, completed\|failed outcome, stats?) -> bool` | as finishRun but `WHERE status IN (queued,running,awaiting_approval)`; "a late 'completed' never overwrites the user's cancel" |
| `isCancelled` (205-212) | `(id) -> bool` | `SELECT status … ` == 'cancelled' (not user-scoped) |
| `reapInterruptedRuns` (225-249) | `() -> number` | `UPDATE runs SET status='failed', error='Interrupted: the worker stopped while this was running. Start it again to retry.', finished_at=now, updated_at=now WHERE status='running' RETURNING id,user_id,kind`; for each emit `run.failed` error "This run was interrupted because the worker stopped. Everything it finished was kept; start it again to pick up the rest." Queued runs and awaiting_approval runs are left alone. **Global (all users)**. Since `run.failed` is a follow-up kind, chat-started reaped runs get a chat follow-up. |

Cancellation is cooperative (runs.ts:102-112): handlers check `isCancelled` between stages/batches.

Status transitions overall:
```
queued --(handler setRunStatusIf/startRun)--> running
running --(requestApproval approve_each)--> awaiting_approval
awaiting_approval --(continuation resumeFrom)--> running
queued|running|awaiting_approval --(requestCancel)--> cancelled
queued|running|awaiting_approval --(finishActiveRun)--> completed|failed
running --(reaper at boot)--> failed
(finishRun is unconditional: tailor/render handlers can overwrite cancelled)
```

---

## 5. Events (src/lib/runs/events.ts)

`emit(input: { userId; kind; message; runId?; level?='info'; data?; approvalId? }) -> AgentEvent` (events.ts:18-50):
1. `INSERT INTO agent_events (user_id, run_id, level, kind, message, data, approval_id) RETURNING *` (nulls for absent).
2. If `event.run_id` and `kind ∈ FOLLOW_UP_KINDS` → `queueFollowUp(event)`.

`FOLLOW_UP_KINDS` (chat/follow-up-kinds.ts:10-16): `approval.requested`, `run.completed`, `run.failed`, `tailor.done`, `tailor.failed`. "Hooked here rather than at each call site, so a new kind of run follows up without anyone remembering to wire it." (events.ts:42-44). CLAUDE.md: "A new run kind should end with run.completed or run.failed (or add its own final kinds there)".

`queueFollowUp` (events.ts:57-74): `SELECT conversation_id FROM runs WHERE id = run_id`; if null return (button-started runs have no conversation). Else `send('chat.follow_up', { userId, runId, eventId: event.id }, { singletonKey: 'follow-up-<id>' })`. Any error caught and logged — "A failure here must never fail the run that emitted the event."

Progress is "an append-only table, not a socket… progress survives a page reload, a worker restart and a browser that was closed for an hour" (events.ts:10-16).

---

## 6. Approvals and the continuation pattern (src/lib/runs/approvals.ts)

Concept (approvals.ts:12-26): a pipeline at a decision point writes an `approvals` row containing its `continuation` (`{ queue, payload }`), pauses its run and returns. Deciding (UI or chat) enqueues the continuation. Autopilot writes the same row already `auto_approved` and enqueues at once — one code path. **Every decision enqueues the continuation, a "no" included** (the continuation is what finishes the run); continuations treat anything but approved/auto_approved as no (`isApproved`).

Constants: `APPROVAL_TTL_MS = 7*24*60*60*1000` (approvals.ts:29).

Payload types:
- `CostEstimate = { usd: number|null; perItemUsd: number|null; model: string }` (`provider:model` or `"unconfigured"`) (58-64)
- `DeepEvalBatchItem = { matchId; jobId; title; company; fastScore: number|null }` (66-72)
- `DeepEvalBatchPayload = { items: DeepEvalBatchItem[]; estimate: CostEstimate; note: string|null }` (75-80)
- `ShortlistItem = { matchId; jobId; title; company; deepScore: number|null; recommendation: 'strong'|'apply'|'maybe'|'skip'; summary: string }` (82-90)
- `ShortlistPayload = { items: ShortlistItem[] }` (93-95)
- `ApprovalDecision = { approved: boolean; itemIds?: string[] }` (40-44); `StoredDecision` same shape, `itemIds` present whenever payload has items: approved subset in payload order, or `[]` for rejection (46-54).
- Item ids: `matchId`, else `id` (payloadItemIds, 401-410), deduped, payload order.

### 6.1 `requestApproval(input)` (99-237)
Input: `{ userId, runId, kind, title, payload, continuation: Continuation | (approvalId)=>Continuation, mode, stats?, messages?: {requested?, auto?}, data? }`.
- `id = randomUUID()`; continuation resolved with id.
- **autopilot** (151-184): `INSERT approvals (id,user_id,run_id,kind,title,payload,continuation,status='auto_approved', decision = hasItems ? {approved:true,itemIds:all} : {approved:true}, decided_via='autopilot', decided_at=now)` (no expires_at). If stats → merge into run stats. Emit `approval.auto` info, message `messages.auto ?? "Autopilot approved: <title>."`, approvalId, data `{ kind, ...data }`. Then `enqueue(continuation)`. Run stays `running`.
- **approve_each** (186-236): insert `status='pending', expires_at = now + 7d`. Then conditional pause: `UPDATE runs SET status='awaiting_approval', stats = stats || $stats, updated_at=now WHERE id AND user_id AND status IN (queued,running,awaiting_approval) RETURNING id`. If no row (cancelled first): `UPDATE approvals SET status='expired' WHERE id AND status='pending'`, return `{...approval, status:'expired'}` with no event. Else emit `approval.requested` info, message `messages.requested ?? "Waiting for your go-ahead: <title>."`, approvalId, data `{kind, ...data}` (FOLLOW_UP kind).

### 6.2 `decideApproval(userId, approvalId, decision, via: 'ui'|'chat') -> DecideResult` (255-359)
`DecideResult = {ok:true, approval} | {ok:false, reason:'not_found'|'already_decided'|'expired'}`.
1. Non-UUID → not_found. Load row by (id,user_id) → not_found if missing.
2. status expired → expired; status ≠ pending → already_decided.
3. Past expiry (`expires_at <= now`, JS clock) → `expireStaleApprovals(userId)` then expired.
4. Load run status; if run missing or not active → `UPDATE approvals SET status='expired' WHERE id AND status='pending'`; return expired.
5. `stored = resolveDecision(payload, decision)`; `status = stored.approved ? 'approved' : 'rejected'`.
6. Conditional write: `UPDATE approvals SET status, decision=stored, decided_via=via, decided_at=now WHERE id AND user_id AND status='pending' AND (expires_at IS NULL OR expires_at > now()) RETURNING *`.
7. If no row (lost race): re-read; not_found / `expired` if expired or pending-past-expiry / else already_decided.
8. Emit `approval.decided` (before enqueue: "the feed should read 'approved' before it reads 'evaluating'"), message `describeDecision`, data `{ kind, via, approved, itemIds: stored.itemIds ?? null }`.
9. `enqueue(decided.continuation)`; on failure: revert `UPDATE approvals SET status='pending', decision=null, decided_via=null, decided_at=null WHERE id AND status=$status`; emit `approval.reopened` warn "Could not resume the run, so the request is open again." (errors swallowed); rethrow.
Consumers: server action src/app/actions/approvals.ts:40 (via 'ui'), chat tool src/lib/agents/tools/approvals.ts:112 (via 'chat').

### 6.3 Pure helpers
- `resolveDecision(payload, decision)` (365-379): no items → `{approved}`; rejected → `{approved:false,itemIds:[]}`; approved: `itemIds = decision.itemIds ? all.filter(in wanted) : all`; empty → `{approved:false,itemIds:[]}` (empty subset = rejection; unknown ids ignored).
- `isApproved(approval)` (382-387): status ∈ {approved, auto_approved} and `decision?.approved !== false`.
- `approvedItemIds(approval)` (390-398): `decision.itemIds` (strings only) if array else all payload ids.
- `describeDecision(approval, decision)` (421-444): `total = payload ids count`, `chosen = itemIds?.length ?? total`, `count = chosen===total ? roles(chosen) : "<chosen> of <roles(total)>"`; deep_eval_batch: "Approved deep evaluation of {count}." / "Skipped the deep evaluation."; shortlist_proposal: "Approved shortlisting {count}." / "Left the shortlist as it was."; default "Approved: {title}." / "Declined: {title}.". `roles(n) = "n role"|"n roles"`.

### 6.4 Reading
- `listPendingApprovals(userId)` (459-482): first `expireStaleApprovals(userId)` (errors logged); then `approvals JOIN runs ON runs.id = approvals.run_id WHERE approvals.user_id AND status='pending' AND (expires_at IS NULL OR expires_at > now()) AND runs.status IN active ORDER BY approvals.created_at DESC`. Used by the chat tool.
- `getApproval(userId, id)` (488-498): pending past expiry reads as `expired` (no write).
- `listRunApprovals(userId, runId)` (501-516): oldest first, same expiry mapping. (Unused outside tests.)

### 6.5 Expiry
- `expireRunApprovals(runId) -> count` (537-544): `UPDATE approvals SET status='expired' WHERE run_id AND status='pending' RETURNING id`. No continuation (run was cancelled).
- `expireStaleApprovals(userId) -> count` (553-583): `UPDATE approvals SET status='expired' WHERE user_id AND status='pending' AND expires_at <= now() RETURNING *`; for each emit `approval.expired` warn "No answer within a week, so this lapsed: <title>." with approvalId, then enqueue its continuation (an expiry is a "no" by timeout; the continuation finishes the run); enqueue errors logged. Callers: `/api/live` on first poll (`since == 0`, src/app/api/live/route.ts:21), `listPendingApprovals`, `decideApproval`.
- There is **no scheduled sweeper**: lapsed approvals are only expired when the user loads a page or asks chat.

### 6.6 Continuation validation — `parseContinuation` / `enqueue` (601-616)
Value must be an object with `queue` ∈ known queue names and `payload` an object, else throws "This approval has no valid continuation to resume.". Then `send(queue, payload)` (no options). **Contract:** stored continuations in existing rows reference wire queue names (`search.evaluate`, `search.shortlist`).

---

## 7. Live snapshot (src/lib/runs/live.ts) — consumed by `GET /api/live`

`liveSnapshot(userId, since: number) -> LiveSnapshot` (96-172). Four parallel reads:
1. events: `since > 0` → `SELECT * FROM agent_events WHERE user_id AND id > since ORDER BY id LIMIT 200`; else latest 60 by id desc, reversed to ascending.
2. runs: `WHERE user_id AND (status IN (queued,running,awaiting_approval) OR updated_at > now-10min) ORDER BY created_at DESC LIMIT 20` (`RECENT_MS = 10*60*1000`, live.ts:69).
3. approvals: `WHERE user_id AND status='pending' AND (expires_at IS NULL OR expires_at > now()) ORDER BY created_at DESC LIMIT 20` (not joined to runs).
4. `workerStatus()`.

Shape (JSON contract with `useLive()` client):
```
LiveSnapshot = { cursor: number /* last event id, else since */, events: LiveEvent[], runs: LiveRun[], approvals: LiveApproval[], worker: WorkerStatus }
LiveEvent = { id, runId|null, level, kind, message, data, approvalId|null, createdAt: ISO }
LiveRun = { id, kind, status, mode, prompt: input.prompt ?? null,
            summary: kind==='search' ? describeBrief(parseBrief(input.brief ?? input.hints)) : null,
            stats: stats ?? {}, output|null, error|null, createdAt, startedAt|null, finishedAt|null }  (toLiveRun 71-94)
LiveApproval = { id, runId, kind, title, payload ?? {}, createdAt }
WorkerStatus = { online: boolean, lastSeenAt: ISO|null, pid: number|null }
```
Route (`src/app/api/live/route.ts:12-26`): `since = Number(query.since ?? 0)`, finite and >0 else 0; when 0 call `expireStaleApprovals(user)` first; respond with `Cache-Control: no-store`. `toLiveRun` is also used by `/api/runs/[id]` and the activity page.

---

## 8. Heartbeat (src/lib/system/heartbeat.ts)

- `KEY = "worker"`; `WORKER_STALE_MS = 45_000` (15-18).
- `beat({ pid, startedAt, concurrency })` (26-34): `INSERT INTO system_state (key, value, updated_at) VALUES ('worker', $v, now) ON CONFLICT (key) DO UPDATE SET value=$v, updated_at=now()`. Called every 10 s.
- `workerStatus()` (42-57): row missing → `{online:false,lastSeenAt:null,pid:null}`; else `online = Date.now() - updated_at < 45000`, `lastSeenAt = updated_at ISO`, `pid = value.pid ?? null`.
- Why: "A search enqueued while the worker is down sits in `queued` forever, and from the user's side that is indistinguishable from a slow search" (7-13).

---

## 9. Search run state machine

Files: worker/handlers/search.ts, lib/runs/search-run.ts, lib/runs/evaluate-run.ts, worker/handlers/evaluate.ts.

### 9.1 Creation (outside the worker)
`runs` row `{ kind:'search', mode, status:'queued', input:{ prompt, hints }, conversationId? }` then `send('search', { userId, runId })`.
- Button: actions/runs.ts:22-87 — prompt = trimmed prompt || previous run's prompt || `SAVED_PREFERENCES_PROMPT` ("Search with my saved preferences", brief.ts:47); hints = previous run's hints or null; mode = input.mode ?? earlier.mode ?? settings.defaultMode. Refused if `assertCanSearch` throws or `activeSearch` exists.
- Chat tool: agents/tools/search.ts:91-134 — `input: { prompt, hints: cleanBrief(hints) }`, conversationId set.

`SearchRunInput` (search-run.ts:87-106) keys in `runs.input`: `prompt?`, `hints?: Partial<SearchBrief>`, `brief?: SearchBrief` (cached), `criteria?: DerivedCriteria` (cached), `skipRefresh?: boolean` (never set by producers), `candidateJobIds?: string[]` (the prefilter's choice; what continuations mean by "this run's matches").

### 9.2 Handler `handleSearch` (handlers/search.ts:14-28)
For each job: `getRun(userId, runId)`; missing → log and skip; `status==='cancelled'` → skip silently (no event); else `executeSearchRun(run)`.

### 9.3 `executeSearchRun(run)` (search-run.ts:140-352) — phases in `stats.phase`
Phase sequence (search-run.ts:58-77): starting → refreshing → aggregating → filtering → scoring → awaiting_evaluation | evaluating → awaiting_shortlist → done.

| Step | Code | Writes / events |
|---|---|---|
| 0 start | 145-157 | `setRunStatusIf(id, [queued,running], 'running', {phase:'starting'})` — false (cancelled) → return. Emit `run.started`: "Searching: <prompt>" or "Searching for matching roles" |
| (try) | 159 | everything below inside try; catch → `failRun(run, error)` |
| 1 settings + gate | 160-161 | `getSettings(userId)` (defaultMode, scoreThreshold=55, deepEvalTopN=15; settings.ts:18-28). `assertCanSearch(userId)` (122-138): profile.preferences/interview_state parsed; `interviewProgress(...).blocking` non-empty → throw `PreferencesIncompleteError(missing labels)` message "Cannot search yet: <a, b> not known" → run fails |
| 1 derive | 165-176 | if `input.brief && input.criteria` reuse (`parseBrief(input.brief)`), else `deriveSearch({ preferences: saved, request: requestText(prompt), hints, context:{userId, runId} })` (LLM purpose "score", ranking/prefilter.ts:118-175). `saveInput(run.id, derived)` = `UPDATE runs SET input = input \|\| {brief, criteria}::jsonb` (550-558). `preferences = applyBrief(saved, brief)`; `described = describeBrief(brief)` |
| emit | 182-190 | `search.criteria`: "Looking for <lowerFirst(described)>." or "Looking for roles that fit your saved preferences." + " Titles like: <first 6 title_include joined ', '>."; data `{ brief, criteria }`. `lowerFirst` lowercases first char only if `/^[A-Z][a-z]/` (543-547) |
| cancel check | 192 | `isCancelled` → emit `run.cancelled` warn "Search cancelled. Everything found so far was kept." and return (cancelled() 464-472) |
| 2 targets | 195-200 | if `brief.companies` → `resolveTargets(run, names)` (480-536): `findCompaniesByNames(names)` (queries/companies.ts:164-199); for each missing name `discoverCompany({ nameOrUrl, addedByUserId: userId, discoveredVia:'user' })` (errors → missing). mergeStats `{ companies: found names, companiesMissing }`. Emit `search.companies.added` "Started tracking <joinNames(added)>." and/or `search.companies.missing` warn "Couldn't find a job board for <names>, so its/their roles only come from the job aggregator. Ask Foray to add the company with its careers page link to search it directly." `companyFilter = { ids: found ids, names: brief.companies }` |
| cancel check | 202 | |
| 3 refresh | 206-239 | mergeStats `{phase:'refreshing'}`. Unless `input.skipRefresh`: `cutoff = staleCutoff()` (now − 2 h); stale = targets ? found companies with `last_fetched_at` null or < cutoff : `staleCompanies(cutoff)` (limit 200). If any: emit `source.refresh.start` ("Refreshing <names>" or "Refreshing N stale job boards"); `refreshCompanies(stale)` (pool of 6); mergeStats `{ boardsRefreshed: ok, boardsFailed: total-ok }`; emit `source.refresh.done` "Refreshed <ok>/<n> board(s)" |
| cancel check | 241 | |
| 4 aggregator | 245-248 | only if no targets or some targets missing: mergeStats `{phase:'aggregating'}`; `runAggregator(...)` (566-627, below) |
| cancel check | 250 | |
| 5 prefilter | 253-274 | mergeStats `{phase:'filtering'}`; `prefilterJobs({ userId, preferences, criteria, companies: companyFilter })` (ranking/prefilter.ts:298; default limit 600, MAX_AGE_DAYS 45 when no company filter). mergeStats `{ activeInMirror, candidates: selected }`. Emit `prefilter.done` "<selected> of <activeInMirror.toLocaleString()> open postings match this search/your criteria[ (capped)]", data = funnel `{activeInMirror, selected, cappedAt}` |
| 6 matches | 277-283 | `upsertMatches(userId, jobIds)` (636-667) → `created`; `saveInput({ candidateJobIds: jobIds })`; mergeStats `{ matches: jobIds.length, newMatches: created }` |
| cancel check | 285 | |
| 7 scoring | 288-345 | mergeStats `{phase:'scoring'}`; `resume = ResumeSchema.safeParse(getBaseResume().data)`; emit `scoring.start` "Scoring N roles against your profile"; `scoreMatches({ userId, preferences, focus: brief.focus, resume, jobIds, threshold: settings.scoreThreshold, context, shouldStop: isCancelled, onProgress: (done,total) => mergeStats{scored:done} + emit scoring.progress debug "Scored <done> of <total>" })` (fast-score.ts:111-249: skips already-scored, batches of 40, 16 concurrent LLM calls, archives `stage found` matches below threshold with note "Auto-archived: scored X, below the T threshold."). mergeStats `{ scored, autoArchived: archived, scoringFailed: failed }`. Cancel check. Emit `scoring.done` (329-345): no jobIds → "Nothing to score."; scored==0 && failed==0 → "Nothing new to score: every one of these was scored before."; else "Scored N role(s)" + ("; A came in under T and was/were archived." or ".") + (" F could not be scored." if F>0) |
| 8 gate 1 | 348 | `gateDeepEvaluation(run, settings, jobIds, brief)` |

`upsertMatches` (636-667): batches of 500: `SELECT job_id FROM job_matches WHERE user_id AND job_id IN batch`; insert only the fresh ones `(user_id, job_id, stage='found') ON CONFLICT DO NOTHING`; `created += fresh.length` (counts intended inserts, not actual). Existing matches untouched ("a job the user has already shortlisted or applied to must not be dragged back to found").

`runAggregator(run, preferences, criteria, brief)` (566-627):
- `role = preferences.target_roles?.[0] ?? criteria.title_include[0] ?? "software engineer"`; `level = "entry level "` for seniority entry/new_grad, `"intern "` for intern, else ""; `company = " at <brief.companies[0]>"` if any; `where = " in <preferences.locations[0]>"` if any; `query = level+role+company+where`.
- `searchJSearch({ query, maxRequests: MAX_JSEARCH_REQUESTS=3 (search-run.ts:85), remoteOnly: preferences.remote_pref === 'remote' })`.
- `ingestJobs({ jobs })` (no company, no deactivation). `registerBoardsFromApplyLinks(discoveredBoards, { addedByUserId })`.
- mergeStats `{ aggregatorJobs, aggregatorRequests: requestsUsed, companiesDiscovered }`; emit `source.aggregator.done` "Aggregator: N postings for \"<query>\"[, K new job boards registered]", data `{ requestsThisMonth }`.
- Any error → emit `source.aggregator.skipped`, level `info` for JSearchQuotaError/JSearchDisabledError else `warn`, message = error.message. **Never fails the run.**

### 9.4 Gate 1 — `gateDeepEvaluation(run, settings, candidateJobIds, brief)` (search-run.ts:361-462)
1. `settings.deepEvalTopN <= 0` → `completeSearchRun(run)`; return.
2. `selectForDeepEval(userId, { jobIds: candidateJobIds, minFastScore: settings.scoreThreshold, limit: deepEvalTopN })` (queries/matches.ts:359-391): this run's postings only, stage in (found, shortlisted), `fast_score >= threshold`, `deep_eval IS NULL`, `jobs.is_active`, ordered fast_score desc, posted_at desc nulls last; returns `{matchId, jobId, title, company, fastScore, descriptionChars = coalesce(length(description_text),0)}`. Empty → complete.
3. `loadEvalCandidate(userId, { brief })` null (no resume) → emit `evaluation.skipped` warn "Skipped the deep evaluation: there is no resume on file to judge against." → complete.
4. `estimate = estimateEvalCostFor(candidate, descriptionChars[])`; `estimate.model === 'unconfigured'` → emit `evaluation.skipped` warn "Skipped the deep evaluation: no model is configured for it (LLM_EVALUATE or LLM_DEFAULT)." → complete.
5. `cost = costPhrase(estimate.usd)` ("under a cent" if < 0.01, "about $X.XX", or null); `priced = cost ? " (cost)" : ""`. `note` = "<model> has no price on file, so there is no cost estimate." when usd null, else null.
6. `requestApproval({ kind:'deep_eval_batch', title: "Deep-evaluate <n role(s)><priced>", payload: {items, estimate, note}, mode: run.mode, continuation: approvalId => ({ queue:'search.evaluate', payload:{ userId, runId, approvalId } }), stats: autopilot ? {phase:'evaluating', toEvaluate:n} : {phase:'awaiting_evaluation', toEvaluate:n}, messages: { requested: "Waiting for your go-ahead to deep-evaluate <what><priced>.", auto: "Autopilot: deep-evaluating <what><priced>." }, data: { count:n, estimate } })`.
Handler returns. approve_each: run now `awaiting_approval`. Autopilot: run still `running`, continuation queued.

### 9.5 Continuation 1 — `continueSearchEvaluate({userId, runId, approvalId})` (evaluate-run.ts:231-372), queue `search.evaluate`
`RESUMABLE = [queued, running, awaiting_approval]` (90).
1. `getRun` missing → log return. Status not resumable → return (cancelled, or finished by an earlier delivery).
2. (try) `getApproval`; missing or `approval.runId !== run.id` → throw "The approval this search was waiting on no longer exists." (→ failRun). `status==='pending'` → warn log, return.
3. Not approved → emit `evaluation.skipped` info ("The deep evaluation was never answered, so it was skipped." if expired else "Skipped the deep evaluation, as you asked."), approvalId; mergeStats `{evaluated:0}`; `completeSearchRun(run)`; return.
4. `resumeFrom(run.id, approvalId, {phase:'evaluating'})` (818-839): `UPDATE runs SET status='running', stats = stats || {phase, resumedFrom: approvalId}, updated_at WHERE id AND status IN RESUMABLE AND (stats->>'resumedFrom') IS DISTINCT FROM approvalId RETURNING id` — idempotent against pg-boss double delivery; false → return.
5. `batch = approvedItemIds(approval)`; `candidate = loadEvalCandidate(userId, { brief: parseBrief(run.input.brief) })`; null → emit `evaluation.skipped` warn (no resume) → complete.
6. `toEvaluate = matchesNeedingEvaluation(userId, batch)` (matches.ts:398-419: UUIDs, this user, deep_eval null, stage in open stages, input order kept). mergeStats `{ toEvaluate: len, evaluated: 0, evaluationFailed: 0 }`.
7. If len > 0: emit `evaluation.start` "Deep-evaluating <roles> against your resume"; `evaluateBatch(...)` (below); mergeStats `{evaluated, evaluationFailed}`; if cancelled → `reportCancelled(run, evaluatedNow)` return; if 0 evaluated → throw "Deep evaluation failed for every role: <first error | unknown error>"; if some failed → emit `evaluation.failed` warn "<roles(F)> could not be evaluated and was/were left as it was/they were: <first error>", data `{ failed: [{matchId, error}] }`.
8. `rows = getMatchRows(userId, batch ∩ UUID)` (whole approved batch, best first by coalesce(deep,fast) desc nulls last); `recommended = count(recommendation in strong|apply)`; mergeStats `{recommended}`; emit `evaluation.done` `describeVerdicts(rows)` ("Deep evaluation done: S strong, A worth applying, M maybe(s), K to skip." or "Deep evaluation done.").
9. cancel check → reportCancelled; else `proposeShortlist(run, rows)`.
10. catch → `failRun(run, error)`.

`evaluateBatch({userId, runId, matchIds, candidate})` (665-733): `pooled(matchIds, fn, EVAL_CONCURRENCY=5)`; per item: if already cancelled skip; `isCancelled` → set cancelled, skip; else `evaluateMatch({ userId, matchId, candidate, context:{userId, runId} })` (deep-eval.ts:359-413: LLM purpose "evaluate", writes deep_score/deep_eval, skip verdict archives a `found` match with note "Archived: the deep review recommended skipping this one."); failure pushes `{matchId, error}` and continues. Progress every `PROGRESS_EVERY=3` completions or at the end: chained (serialized) `mergeStats {evaluated, evaluationFailed}` + emit `evaluation.progress` debug "Evaluated <done> of <total>" — chained "so they land in order"; progress errors logged only.

`reportCancelled(run, kept)` (794-805): emit `run.cancelled` warn: kept>0 → "Stopped. The <roles> already evaluated was/were kept." else "Stopped before anything was evaluated."

### 9.6 Gate 2 — `proposeShortlist(run, rows)` (379-423)
`picks = rows.filter(isShortlistCandidate)` (stage `found` and recommendation strong|apply; matches.ts:425-432). None → `completeSearchRun`. Else `requestApproval({ kind:'shortlist_proposal', title: "Shortlist <roles(n)>", payload: { items: picks.map(row => ({ matchId, jobId, title, company, deepScore, recommendation: row.recommendation ?? 'apply', summary: row.headline ?? '' })) }, continuation: approvalId => ({ queue:'search.shortlist', payload:{userId, runId, approvalId} }), stats: autopilot ? {proposedShortlist:n} : {phase:'awaiting_shortlist', proposedShortlist:n}, messages: { requested: "<1 role looks|n roles look> worth applying to. Shortlist it/them?", auto: "Autopilot: shortlisting <roles> that looks/look worth applying to." }, data: {count:n} })`.

### 9.7 Continuation 2 — `continueSearchShortlist(payload)` (432-495), queue `search.shortlist`
Same prologue as 9.5 (missing run, not resumable, approval missing → throw, pending → ignore). Not approved → emit `shortlist.skipped` info ("The shortlist proposal was never answered, so nothing was moved." / "Left the shortlist as it was, as you asked."), approvalId; mergeStats `{shortlisted:0}`; complete. Else `resumeFrom(run.id, approvalId)` (no phase); `moved = moveMatchesFromStage(userId, approvedItemIds, 'found', 'shortlisted')` (matches.ts:451-473: single UPDATE … WHERE stage='found' RETURNING id; sets stage_changed_at, updated_at); mergeStats `{shortlisted: moved.length}`; emit `shortlist.done` ("Shortlisted <roles>." or "Nothing to shortlist: those roles had already been moved."), data `{ matchIds: moved }`; `completeSearchRun`. catch → failRun.

### 9.8 `completeSearchRun(ref) -> bool` (101-149)
Reload run; `count(key)` = finite number in stats else 0; `candidateJobIds = input.candidateJobIds` (strings). Output:
```
SearchRunOutput = { candidates, newMatches, scored, autoArchived, evaluated, shortlisted,
                    topMatchIds: topMatchIdsForJobs(userId, candidateJobIds, 5) }
```
`topMatchIdsForJobs` (matches.ts:479-501): open stages, score not null, order coalesce(deep,fast) desc, updated_at desc, limit 5.
`bands = evaluated > 0 ? {strong:0,good:0} : scoreBandsForJobs(...)` (matches.ts:504-525: open matches, strong = score>=80, good = 65<=score<80).
`finishActiveRun(id, {status:'completed', output}, {phase:'done'})`; false → return false (cancelled). Emit `run.completed` message `summarizeSearchRun({...output, recommended: count('recommended'), ...bands})`, data = output.

`summarizeSearchRun` (155-202): candidates 0 → "No open roles matched your criteria this time."; `found = "Found <roles(c)>" + ("" if newMatches >= c, " (none new)" if 0, " (<n> new)")`; `done = [found, "scored X" if X>0, "deep-evaluated E" if E>0].join(", ")`; if E>0: `done. ` + ("None looked worth applying to." | "R look(s) worth applying to" + ("; S shortlisted." | ".")); else `done.` + optional " " + capitalize(join of "S look(s) strong", "G is/are worth a look", "A was/were archived as too weak") + ".".

`failRun(ref, error)` (205-222): message = error.message or String; log; `finishActiveRun(failed)`; if moved emit `run.failed` error with message.

### 9.9 Backstop (worker/handlers/evaluate.ts:49-62)
`search.evaluate`, `search.shortlist`, `match.evaluate` handlers wrap work in try/catch → `failRun({id: runId, userId}, error)`; errors from failRun logged. Each handler logs `[worker] search.evaluate run <id>` etc.

### 9.10 Search run stats/output key catalogue (UI reads them: run-card.tsx:57-106, run-summary.ts:53-80)
stats: `phase`, `companies`, `companiesMissing`, `boardsRefreshed`, `boardsFailed`, `aggregatorJobs`, `aggregatorRequests`, `companiesDiscovered`, `activeInMirror`, `candidates`, `matches`, `newMatches`, `scored`, `autoArchived`, `scoringFailed`, `toEvaluate`, `evaluated`, `evaluationFailed`, `recommended`, `proposedShortlist`, `shortlisted`, `resumedFrom`.
output: `SearchRunOutput` above.

---

## 10. On-demand deep evaluation (`rank_deep`)

### 10.1 Start — `startMatchEvaluation(userId, matchIds, { conversationId? })` (evaluate-run.ts:515-563) — runs in Next
1. `getMatchRows(userId, unique(matchIds) ∩ UUID)`; none → throw `UnknownMatchError` ("There is no such match to evaluate.").
2. `loadBaseResume(userId)` null → throw `NoResumeError` ("There is no resume on file to evaluate against. Upload one on the profile page first.").
3. `ids = matches.matchId sorted`. Reuse: `SELECT * FROM runs WHERE user_id AND kind='rank_deep' AND status IN (queued,running) AND input->'matchIds' = $ids::jsonb LIMIT 1` → `{ run, reused:true, matches }`.
4. `createRun({ kind:'rank_deep', status:'queued', input:{ matchIds: ids }, conversationId })`; `send('match.evaluate', { userId, runId, matchIds: ids })`; on send failure `finishActiveRun(failed, "Could not queue the evaluation.")` and rethrow. No approval gate ("the user asked for exactly this").

### 10.2 Worker — `executeMatchEvaluate(payload)` (566-650)
1. getRun missing → log return. `setRunStatusIf(id, ['queued'], 'running', {phase:'evaluating'})` false → return (cancelled or redelivery).
2. (try) `rows = getMatchRows(userId, unique(payload.matchIds) ∩ UUID)`; empty → throw "There is nothing to evaluate: those matches do not exist."
3. mergeStats `{toEvaluate: n, evaluated:0, evaluationFailed:0}`; emit `run.started`: "Evaluating <title> at <company>" (1 row) or "Deep-evaluating <roles(n)>".
4. `candidate = loadEvalCandidate(userId)` (no brief); null → throw NoResumeError.
5. `evaluateBatch(...)` (re-evaluates even already-evaluated matches — no `matchesNeedingEvaluation` filter here); mergeStats; cancelled → reportCancelled return; 0 evaluated → throw (first error or "Nothing was evaluated.").
6. `output = { evaluated: n, matchIds: evaluated ids }`; `finishActiveRun(completed, output, {phase:'done'})`; false → return; emit `run.completed` `describeEvaluations(evaluated, failedCount)`, data output.
7. catch → failRun.

`describeEvaluations` (748-773): one → "Evaluated <title> at <company>: <score>, <VERDICT>."; many → sort by score desc, top 3 "<label> (<score>, <verdict>)" joined "; " + "; and N more" if >3 → "Evaluated <roles>: …."; + " F could not be evaluated." VERDICT: strong "a strong match", apply "worth applying", maybe "a maybe", skip "probably not worth applying".

---

## 11. Tailor run (`tailor`) + PDF

### 11.1 Start — `startTailorRun(userId, id, { conversationId? }) -> StartTailorResult` (tailor-run.ts:289-356) — runs in Next
`StartTailorResult = {ok:true, runId, matchId, message} | {ok:false, message}`.
1. `getBaseResume` none → `{ok:false, "There is no resume on file to tailor. Upload one on your profile first."}`.
2. `wanted = id.trim()`; `resolveMatch(userId, wanted)` (match id or job id); if none, `SELECT id FROM jobs WHERE id = wanted` → `ensureMatch` (creates `found` match). `getMatchDetail`; none → `{ok:false, "I couldn't find that job."}`.
3. `label = "<job.title> at <job.companyName>"`. In-flight dedupe `activeTailorRun` (265-283): `kind='tailor' AND status IN (queued,running) AND input->>'matchId' = $matchId AND created_at > now-15min` → `{ok:true, runId: active.id, message: "Already tailoring your resume for <label>. It will be ready in about a minute."}` ("A run stuck longer than this … must not block a retry forever").
4. `createRun({ kind:'tailor', mode:'approve_each', status:'queued', input:{ matchId, prompt: "Tailor your resume for <label>" }, conversationId })`; `send('resume.tailor', {userId, runId, matchId})`; failure → `finishRun(failed, "Could not queue the job.")`, `{ok:false, "Could not start tailoring right now. Try again in a moment."}`.
5. `{ok:true, …, message: "Tailoring your resume for <label>. It takes about a minute, and every change is checked against your original resume before it is saved."}`.

### 11.2 Worker — `handleResumeTailor` (handlers/resume.ts:28-88)
1. getRun missing → log continue; `status==='cancelled'` → continue.
2. `tailored = executeTailorRun(run)`; null → continue (failure/cancel already recorded).
3. mergeStats `{phase:'rendering'}`; try `renderStoredResume(userId, tailored.resumeId)`; on error log and emit `render.failed` **warn** "Your tailored resume was saved, but the PDF could not be made. The preview still works; try rendering it again.", data `{resumeId}`; documentId/pages stay null.
4. mergeStats `{phase:'done', documentId, pages}`; **unconditional** `finishRun(run.id, {status:'completed', output:{ resumeId, documentId, pages, changes, unsupported, matchId }})`.
5. Emit `tailor.done` (level warn if unsupported>0 else info) message `doneMessage(label, pages, unsupported)` = "Your resume for <label> is ready." + (" It fits on one page." if pages===1, " It runs to N pages." if pages) + (" U line needs/lines need your review before you send it." if U>0); data `{matchId, resumeId, documentId}`. (`tailor.done` is the final follow-up kind for tailor runs — no `run.completed`.)

### 11.3 `executeTailorRun(run) -> TailoredResult|null` (tailor-run.ts:70-221)
`TailoredResult = { resumeId, matchId, label, changes: number, unsupported: number }`.
1. `startRun(run.id)` (unconditional).
2. (try) no `input.matchId` → TailorError "This tailoring run has no job attached."; `getMatchDetail` none → "That job is no longer among your matches."; `loadBaseResume` none → "There is no resume on file to tailor. Upload one on your profile first."
3. mergeStats `{phase:'tailoring', matchId, title, company, baseVersion}`; emit `tailor.started` "Tailoring your resume for <label>.", data `{matchId}`.
4. `draft = tailorResume({ base, job, match, preferences, context })` (lib/resume/tailor.ts; LLM purpose "tailor"). If draft has no work entries but base does → TailorError "The tailored draft came back without any of your experience, so it was discarded. Try again."
5. cancel check → `cancelled(run)`: emit `run.cancelled` warn "Tailoring cancelled. Nothing was saved."; return null (status already cancelled).
6. mergeStats `{phase:'verifying'}`; `result = verifyTailored(base, draft.resume, draft.changes, context)` (lib/resume/verify.ts; LLM purpose "verify"). `revisions = draft.dropped.map(d => ({...d, source:'rule', action:'dropped', replacement:null}))`.
7. If `result.findings.length>0`: mergeStats `{phase:'revising'}`; `reviseTailored(base, draft.resume, findings)` (deterministic) → final + revisions.
8. `verification = finalizeVerification({base, final, result, revisions, baseResumeId, baseVersion})`; `changes = reconcileChanges({base, final, changes: draft.changes, revisions})`.
9. cancel check → cancelled.
10. `saveTailoredResume(userId, {matchId, resume: final, changes, verification})` (queries/resumes.ts:157-200: transaction: next version = max(version)+1 for (user, tailored, match); insert; `UPDATE job_matches SET tailored_resume_id`).
11. mergeStats `{resumeId, checked, revisedCount, unsupported, changes}`; emit `tailor.verified` level `info` if `verification.passed` else `warn`, message `verifiedMessage` (223-245), data `{resumeId, checked, modelChecked, revised, unsupported, passed}`.
12. catch: if `isCancelled` → return null (silently). Else message = TailorError.message or "Tailoring failed: <msg>"; `finishRun(failed)` (unconditional); emit `tailor.failed` error; return null.

`verifiedMessage`: "Checked <N line(s)> against your resume." + ("Every one is supported by what you wrote." if no revisions and no unsupported) / ("<F> change was/changes were not backed by your resume and was/were put back to your original wording or removed." if F>0) + ("<lines(open)> still needs/need your review before you send it." if open>0) + ("The line-by-line check was unavailable, so rewrites kept your original wording." if verification.modelError).

Tailor stats keys: phase (tailoring, verifying, revising, rendering, done), matchId, title, company, baseVersion, resumeId, checked, revisedCount, unsupported, changes, documentId, pages.

### 11.4 Render run (`resume_render`)
Start — `startResumeRender(userId, resumeId, {conversationId?})` (tailor-run.ts:363-399): `getResume` none → `{ok:false, "That resume no longer exists."}`; `createRun({ kind:'resume_render', mode:'approve_each', status:'queued', input:{ resumeId, prompt: base ? "Render your resume to PDF" : "Render your tailored resume to PDF" } })`; send `resume.render`; on failure finishRun(failed, "Could not queue the job.") and `{ok:false, "Could not start rendering right now. Try again in a moment."}`; success message "Rendering your resume to PDF. It takes a few seconds."

Worker — `handleResumeRender` (handlers/resume.ts:90-132): getRun missing → log; cancelled → skip; `startRun`; mergeStats `{phase:'rendering'}`; try `renderStoredResume` → mergeStats `{phase:'done', documentId, pages}`; `finishRun(completed, {resumeId, documentId, pages})`; emit `render.done` "Rendered your resume to a <pages>-page PDF." data `{resumeId, documentId, pages}`. catch → `finishRun(failed, message)`; emit `render.failed` **error** "Could not render the PDF: <message>" data `{resumeId}`. Note: render.* are not follow-up kinds.

### 11.5 `renderStoredResume(userId, resumeId) -> {documentId, pages}` (handlers/resume.ts:142-186)
1. `getResume` none → throw "That resume no longer exists."; `ResumeSchema.safeParse(row.data)` fails → throw "The stored resume could not be read."
2. `rendered = renderPdf(renderResumeHtml(resume))`; if `pages > 1`: `compact = renderPdf(renderResumeHtml(resume, {density:'compact'}))`; keep compact if fewer pages ("changes type size and spacing only, never content").
3. company = tailored resume with job_match_id → `getMatchDetail(...).job.companyName` else null.
4. `filename = pdfFilename(resume, company)` (189-199): `safe(v) = v.replace(/[\\/:*?"<>|\u0000-\u001f]/g,"").replace(/\s+/g," ").trim()`; base = "<name> - Resume" or "Resume"; + " - <company>" ; + ".pdf".
5. `path = storage.put(storageKey(userId, 'resume_pdf', filename), pdf, 'application/pdf')`.
6. `recordDocument({ userId, kind:'resume_pdf', filename, mime:'application/pdf', sizeBytes, storagePath: path, sha256 })` → `INSERT … ON CONFLICT (user_id, sha256) DO UPDATE SET text_extracted = NULL RETURNING *` (queries/profile.ts:43-62). If returned `storage_path !== path` (identical bytes already stored) → `storage.delete(path)`.
7. `markRendered(userId, resumeId, document.id, 'html')` → `UPDATE resumes SET rendered_document_id, render_engine='html', updated_at WHERE id AND user_id`.

---

## 12. Company jobs

### 12.1 `company.discover` — `handleCompanyDiscover` (handlers/company.ts:19-44)
For each job, for each `name` in `names` (sequential): `discoverCompany({ nameOrUrl: name, addedByUserId: userId, discoveredVia: 'agent' })`; emit with `runId ?? null` (always null today): registered → `company.discovered` info "Registered <name> (<source>[:<slug>])"; else `company.not-found` warn "Could not find a job board for \"<name>\"". data `{ trail }`. No try/catch: a throw fails the job → pg-boss retries once (retryLimit 1) and re-runs **all** names, re-emitting events for ones already done.

### 12.2 `company.refresh` — `handleCompanyRefresh` (handlers/company.ts:46-93)
`{userId, runId, all} = job.data ?? {}` ("The cron sends no payload at all, so `data` is null there … destructuring it directly crashed every scheduled run"). targets = `all ? listCompanies({status:'active'})` (limit 500, ordered by name) `: staleCompanies(staleCutoff())` (active AND (last_fetched_at null OR < now-2h) ORDER BY last_fetched_at ASC NULLS FIRST LIMIT 200). Empty → log "nothing stale". Else `refreshCompanies(targets)`; `ok` outcomes; `seen = Σ ok.jobs`; `deactivated = Σ ok.ingest.deactivated`; message "Refreshed <ok>/<n> boards — <seen> postings, <deactivated> closed"; logged with seconds. If userId: emit `company.refresh.done` (runId ?? null) with data `{ boards, ok, notFound, errored }`.

### 12.3 `ping` — `handlePing` (handlers/ping.ts:12-22)
Emit `worker.ping` info message `note ?? "Worker is alive."`, data `{ jobId: job.id, workerPid: process.pid }` (no runId). Log.

### 12.4 `chat.follow_up` — `handleChatFollowUp` (handlers/chat.ts) → `runChatFollowUp(payload)` (chat/follow-up.ts:63-171); logs outcome posted/skipped.
Constants: `REPLY_WAIT_MS = 90_000`, `REPLY_POLL_MS = 1_500` (60-61); agent stream `timeout: 90_000` (141).
1. Load event `(id=eventId, user_id)`; `getRun`; missing event or run without conversation → "skipped" (no announce).
2. `getConversation(userId, run.conversationId)` none → skipped.
3. `skip()` = `announce({skipped:true})` → "skipped". Skip if run cancelled; if event is `approval.requested` and its approval (via `getApproval`) is not pending; if `alreadyFollowedUp(conversationId, eventId)` (`messages.metadata->>'kind'='run_update' AND (metadata->>'eventId')::bigint = eventId`).
4. Wait while `replyInFlight(conversationId)` (last message is role user, not a run_update, created < 90 s ago) and < 90 s elapsed, polling every 1.5 s (in-process sleep, not re-queue: "a delayed job waits for the worker's slow poll, which added half a minute").
5. `proposed = proposedRoles(userId, approval)` (first 10 payload item ids → getMatchRows, payload order; `{title, company, score: deep ?? fast, headline, locations: first 3 joined ", "}`).
6. `update = describeUpdate(run, event, approval, proposed)` (276-389) → `{label, text}`; `notice` = UI message `{ id: generateId(), role:'user', parts:[{type:'text', text}], metadata: { kind:'run_update', runId, eventId, label } }`.
7. `history = loadMessages`; `saveMessages([notice])`.
8. try: `chatAgent(userId, {conversationId, userName, followUp: true})` (read-only tools, FOLLOW_UP_TOOLS); `withLlmContext({userId}, createAgentUIStream({agent, uiMessages: [...history, notice], originalMessages, timeout: 90_000, generateMessageId, messageMetadata: () => ({kind:'follow_up', runId, eventId}), onEnd: capture responseMessage}))` + `consumeStream` (error rethrown). Empty reply (no non-blank text part and no `tool-*` part) → throw "The follow-up came back empty.". `saveMessages([reply])`; `announce({messageId, label})` → "posted".
9. catch: log; `DELETE FROM messages WHERE id = notice.id` ("An update with no reply would sit in the history as an unanswered prompt"); `announce({failed:true})` → "skipped".
`announce` emits `chat.followup` (FOLLOW_UP_EVENT) level debug, **no runId**, message "Foray followed up in chat." if messageId else "No chat follow-up was needed.", data `{ conversationId, runId, ...data }`.
`describeUpdate` labels/prompts: deep_eval gate "Search paused for your go-ahead" (292-305), shortlist gate "Deep review done" (307-323), other gate "Waiting for your go-ahead" (325-330), failures "<KIND_TITLE> stopped" (332-342), search finished "Search finished" (344-359), rank_deep "Deep review finished" (361-371), tailor "Tailored resume ready" (373-383), default "<KIND_TITLE> finished" (385-388). Header line `[Automatic update about run <id>, not written by the user]`; RULES text at 266-270. (Full prompt-text mapping belongs to the chat map.)

---

## 13. Sources: shared types and HTTP

### 13.1 Types (sources/types.ts)
```
NormalizedLocation = { raw: string; city|null; region|null; country|null; metro|null }
NormalizedSalary   = { min: number|null; max: number|null; currency: string|null; period: 'year'|'month'|'hour'|null }
NormalizedJob = { id; source: 'greenhouse'|'lever'|'ashby'|'jsearch'|'web'; externalId; companySlug; companyName;
  title; locations: string[]; locationNorm: NormalizedLocation|null; remote: 'remote'|'hybrid'|'onsite'|'unknown';
  employmentType|null; salary; descriptionRaw|null; descriptionText|null; url; applyUrl|null; postedAt: Date|null;
  raw: unknown; contentHash; dedupeKey }
FetchBoardResult = { companySlug; jobs: NormalizedJob[] }
JobSourceAdapter = { id; fetchBoard?(slug) }
BoardNotFoundError(source, slug): message `No <source> board for slug "<slug>"` — thrown on 404 only (types.ts:82-90)
```
`BOARD_ADAPTERS = { greenhouse, lever, ashby }` (sources/index.ts:7); `boardAdapter(source)`.
Invariant 3: "No source-specific branching may leak past this boundary" (types.ts:3-9).

### 13.2 HTTP (sources/http.ts)
- `USER_AGENT = "Foray/0.1 (job search agent; +https://github.com/foray) contact: local development"` (10-11).
- `DEFAULT_TIMEOUT_MS = 20_000`, `DEFAULT_RETRIES = 3` (13-14) → up to 4 attempts.
- `HttpError(status, url, body≤2000 chars)`, message `HTTP <status> for <url>` (16-25).
- Retryable statuses: 429, 408, ≥500 (28-30). 404 etc. throw immediately.
- `backoffMs(attempt, retryAfter)` (32-40): numeric `Retry-After` seconds → `min(s*1000, 60_000)`; else `2**attempt * 500 + random()*250` (500 ms, 1 s, 2 s … + jitter). HTTP-date Retry-After falls to exponential.
- `fetchText(url, { timeoutMs?, retries?, signal?, accept?='application/json', headers? })` (56-111): per attempt a fresh timeout signal (combined with caller signal); headers `user-agent`, `accept`, extra. `ok` → body text. Non-ok → HttpError; retry if retryable and attempts remain (sleep backoff w/ Retry-After). Network/timeout errors → retry with backoff (no Retry-After) unless last attempt; caller-initiated abort never retried. Node `fetch` **follows redirects** by default.
- `fetchJson<T>` = `JSON.parse(fetchText(...))` (44-54).

Per-call overrides used: board adapters default; JSearch `timeoutMs 45_000, retries 1, accept application/json, headers x-rapidapi-key/x-rapidapi-host` (jsearch.ts:281-289); careers page `accept "text/html,application/xhtml+xml", retries 1, timeoutMs 15_000` (careers-page.ts:86-90); discovery `tryFetch` same accept, `retries 1, timeoutMs 12_000` (discovery.ts:121-133); Brave search uses raw `fetch` with 12 s timeout, no UA (discovery.ts:155-181).

---

## 14. Board adapters

### 14.1 Greenhouse (sources/greenhouse.ts)
- URL: `https://boards-api.greenhouse.io/v1/boards/<encodeURIComponent(slug)>/jobs?content=true` (43,135). One request, no pagination. 404 → BoardNotFoundError (141-143). Body `{ jobs: GreenhouseJob[] }` (null → []).
- `GreenhouseJob` fields (25-39): `id:number, title, company_name?, absolute_url, content? (entity-escaped HTML), updated_at?, first_published?, location?.name, offices?[{name?, location?}], metadata?[{name?, value?: string|string[]}]`.
- `normalizeGreenhouseJob(slug, job)` (79-129):
  - locations = `splitLocations`: `location.name` split on `/[|;]/`, trimmed, non-empty, de-duped (Set, order preserved); if none, offices' `name ?? location` trimmed non-empty de-duped (53-68). Why: boards use both `|` and `;`, "Splitting on only one of them leaves a compound string … quietly drops the job out of every metro filter".
  - `locationTypeFlag`: metadata entry with name lowercased == "location type"; array values joined " " (71-77).
  - `descriptionRaw = content ?? null`; `cleaned = cleanDescription(descriptionRaw)`.
  - `remote = detectRemote({ location: [locations.join(" "), flag ?? ""].join(" "), title, description: cleaned.text })`.
  - `salary = parseSalary(cleaned.text)` (no structured pay; lives in a `pay-range` block).
  - `postedAt = first_published ?? updated_at` parsed as Date.
  - `id = "greenhouse:<slug>:<String(id)>"`, `externalId = String(id)`, `companyName = company_name?.trim() || slug`, `employmentType = null`, `url = absolute_url`, `applyUrl = null` ("board page is also its application form"), `raw = job`, `descriptionText = cleaned.text || null`.
  - `contentHash({ title, locations, descriptionRaw, salaryMin, salaryMax })`, `dedupeKey({ companyName, title, primaryLocation: location?.city ?? location?.raw ?? null })`.

### 14.2 Lever (sources/lever.ts)
- URL: `https://api.lever.co/v0/postings/<encodeURIComponent(slug)>?mode=json` (56,168). 404 → BoardNotFoundError. Body is an array (non-array → []); `[]` is a real empty board.
- `LeverPosting` (30-54): `id, text (title), createdAt? (epoch ms), country?, workplaceType?, hostedUrl, applyUrl?, categories?{commitment, department, location, team, allLocations[]}, lists?[{text, content}], description?, additional?, salaryRange?{min,max,currency,interval}`.
- `fullDescription` (59-70): parts = [description] + for each list `<h3>${list.text}</h3>` (if text) and `list.content` (if content) + [additional]; joined "\n"; null if no parts. ("`description` is not the description … requirements live in `lists[]`").
- locations = `categories.allLocations?.filter(Boolean) ?? (categories.location ? [location] : [])` — NB: an empty `allLocations: []` yields `[]` and does NOT fall back to `location` (`??` only triggers on null/undefined).
- remote = WORKPLACE[workplaceType.lower()] (remote|hybrid|onsite|on-site→onsite) else `detectRemote({location: locations.join(" "), title: text, description: cleaned.text})`.
- salary: structured if `salaryRange?.min` truthy (0 → no) → `{min, max ?? null, currency ?? null, period: periodFromInterval(interval)}` (`hour`/`month`/`year` substring, case-sensitive `includes` on raw interval like `per-year-salary`); used only if `isPlausibleSalary`, else `parseSalary(cleaned.text)`.
- `companyName = slug` (Lever gives no company name), `employmentType = categories.commitment ?? null`, `url = hostedUrl`, `applyUrl = applyUrl ?? null`, `postedAt = createdAt ? new Date(ms) : null`, id `lever:<slug>:<id>`.

### 14.3 Ashby (sources/ashby.ts)
- URL: `https://api.ashbyhq.com/posting-api/job-board/<encodeURIComponent(slug)>?includeCompensation=true` (59,162). 404 (plain-text "Not Found") → BoardNotFoundError — status checked before JSON parse. Body `{ jobs }`; **filter `isListed !== false`** (unlisted are not public).
- `AshbyJob` (35-55): `id, title, department?, team?, employmentType?, location?, secondaryLocations?[{location}], isRemote?, workplaceType?, isListed?, publishedAt?, jobUrl, applyUrl?, descriptionHtml?, descriptionPlain?, compensation?{ summaryComponents?[{compensationType, interval, currencyCode, minValue, maxValue}], scrapeableCompensationSalarySummary? }`.
- locations = [location, ...secondaryLocations.location] trimmed, non-empty (no de-dupe).
- descriptionRaw = `descriptionHtml ?? descriptionPlain ?? null`.
- remote = WORKPLACE[workplaceType.lower()] (remote|hybrid|onsite) else `detectRemote({location, title, isRemoteFlag: isRemote ?? null, description})`.
- salary = `salaryFrom(job)` (first component with compensationType lower == "salary" and numeric minValue; period from interval lowercased e.g. "1 YEAR"; kept only if `isPlausibleSalary`) ?? `parseSalary(scrapeableCompensationSalarySummary ?? cleaned.text)`.
- companyName = slug; employmentType = `employmentType ?? null`; url = jobUrl; applyUrl ?? null; postedAt from publishedAt; id `ashby:<slug>:<id>`.

---

## 15. PDF rendering (src/worker/pdf.ts)

- `LAUNCH = { headless: true, handleSIGINT: false, handleSIGTERM: false, handleSIGHUP: false }` (15-25) — the worker owns shutdown; Playwright's own handlers would kill the browser mid-drain and exit 130 on SIGINT.
- `RENDER_TIMEOUT_MS = 30_000` (27).
- `launch()` (42-52): `PDF_BROWSER_CHANNEL` (trimmed) set → `chromium.launch({...LAUNCH, channel})`; else try bundled Chromium; if error matches `/Executable doesn't exist|executable doesn't exist|Failed to launch.*ENOENT/i` → retry with `channel: "chrome"` (dev machines lack bundled browsers).
- One shared lazily-launched browser per process (`browserPromise`), dropped on `disconnected` or launch failure (54-72).
- `renderPdf(html) -> { pdf: Buffer, pages }` (80-99): increments `inflight`; `renderOnce`; if error matches `/has been closed|Target closed|browser has disconnected|Browser closed|crashed/i` → `discard()` (close + drop) and retry **once**.
- `renderOnce` (101-128): if not connected, discard and relaunch; `newContext({ javaScriptEnabled: false })`; `context.route("**/*", route => route.abort())` (no network at all: "even markup that slipped past escaping could not fetch a URL or execute"); `newPage`; `setDefaultTimeout(30_000)`; `setContent(html, { waitUntil: "load" })`; `page.pdf({ format: "Letter", printBackground: true, preferCSSPageSize: true })` (template owns size/margins via `@page { size: Letter; margin: 0.5in 0.6in; }`, template.ts:324); `pages = countPdfPages(pdf)`; context always closed.
- `countPdfPages(bytes)` (137-146): decode latin1; count `/\/Type\s*\/Page(?![A-Za-z])/g`; if 0 fall back to max of `/\/Count\s+(\d+)/g`; else 0.
- `closeBrowser()` (149-155), `closeWhenIdle(maxWaitMs = 25_000)` (169-181): waits for inflight==0 or 25 s (unref'd timer) then closes.
- `installShutdownHooks()` (188-199): once per process, `process.once(SIGINT|SIGTERM)` → `closeWhenIdle().finally(() => if no other listeners, re-raise signal)`.

---

## 16. Storage (src/lib/storage/index.ts)

- Interface `Storage { put(key, data: Buffer, contentType) -> path; get(path) -> Buffer; delete(path) }` (21-26). Only local FS implemented (`storage = localStorage`, 73); contentType ignored.
- `root = resolve(env.STORAGE_DIR ?? "./data")` (30). STORAGE_DIR default `./data` (env.ts:76). Web and worker must share it (compose volume `storage-data:/app/data`).
- `storageKey(userId, kind, filename)` (33-40) = `<userId>/<kind>/<Date.now()>-<safe>` where `safe = filename.replace(/[^a-zA-Z0-9._-]/g, "_").slice(-100)` (last 100 chars).
- `resolveWithin(path)` (47-54): `resolve(join(root, path))` must equal root or start with `root + "/"`, else throw "Refusing to access a path outside the storage root: <path>".
- `put`: mkdir -p parent, writeFile, return key (the stored path is the key). `get`: readFile. `delete`: unlink, errors swallowed.
- `sha256(buffer)` hex (75-77).

---

## 17. Cleaning, normalization, dedupe, ingest

### 17.1 clean.ts
- `TAIL_FRACTION = 0.3` (19).
- `BOILERPLATE_OPENERS` (28-72), each `{label, pattern}` tested against a tail paragraph with leading `[\s•\-*]+` stripped. Case-insensitive:
  1. "equal opportunity": `^(?:[\w\s.,'&-]{0,60}\bis\s+an\s+)?equal\s+(?:employment\s+)?opportunity\b`
  2. "equal opportunity (we are)": `^we\s+are\s+an\s+equal\s+(?:employment\s+)?opportunity\b`
  3. "non-discrimination": `^(?:all\s+qualified\s+applicants|we\s+do\s+not\s+discriminate|qualified\s+applicants\s+will\s+receive)\b`
  4. "E-Verify": `^(?:e-verify\b|this\s+employer\s+participates\s+in\s+e-verify)`
  5. "pay transparency": `^(?:pay\s+transparency|the\s+(?:pay|salary)\s+range\s+(?:listed|shown|provided)|compensation\s+range\s+(?:listed|shown))\b`
  6. "accommodation": `^(?:if\s+you\s+(?:need|require)\s+(?:an?\s+)?(?:reasonable\s+)?accommodation|we\s+(?:are\s+committed\s+to\s+)?provid(?:e|ing)\s+reasonable\s+accommodation)`
  7. "fair chance / criminal history": `^(?:pursuant\s+to\s+the\s+(?:san\s+francisco|los\s+angeles)|qualified\s+applicants\s+with\s+(?:arrest|criminal))`
  8. "agency notice": `^(?:(?:no\s+agencies|agencies\s+only)|(?:we\s+do\s+not\s+accept\s+(?:unsolicited\s+)?(?:resumes|agency)))`
  9. "privacy notice": `^(?:(?:for\s+)?(?:california|eu|uk)\s+(?:applicants|residents)|by\s+(?:applying|submitting)[\w\s,]{0,40}\byou\s+(?:consent|acknowledge))`
- `BLOCK_TAGS = /<\/?(?:p|div|br|li|ul|ol|h[1-6]|tr|table|section|article|header|footer|blockquote)\b[^>]*>/gi` (74-75).
- `ENTITIES` (78-94): amp &, lt <, gt >, quot ", apos ', nbsp → plain space U+0020, ndash –, mdash —, rsquo ’, lsquo ‘, ldquo “, rdquo ”, hellip …, bull •, middot ·.
- `decodeEntities(s)` (96-108): `/&(#x?[0-9a-f]+|[a-z]+);/gi`: `#x..` hex → codepoint (`#X` too); `#..` decimal; named via lowercase table; unknown left as-is. (Note `#x` followed by hex parsed with parseInt base 16; decimal branch uses parseInt(…,10) so `&#12a;` parses 12.) Single pass (no double decoding).
- `htmlToText(html)` (111-126): remove `<(script|style)\b[^>]*>[\s\S]*?</\1>` (gi); `<li\b[^>]*>` → `"\n• "`; BLOCK_TAGS → `"\n"`; remaining `<[^>]+>` → ""; decodeEntities; then `\r\n?`→`\n`; `[ \t ]+` → " "; ` *\n *` → `\n`; `\n{3,}` → `\n\n`; trim.
- `cleanDescription(raw) -> {text, removed[]}` (141-184): empty → `{text:"", removed:[]}`. If `/&lt;\/?[a-z]/i` matches → decodeEntities first (Greenhouse escapes HTML). `looksLikeHtml = /<\/?[a-z][\s\S]*>/i`; text = htmlToText or `decodeEntities(...).trim()`. Split paragraphs on `/\n{2,}/`; <2 paragraphs → no removal. `cutoff = text.length * 0.7`; walk paragraphs with `consumed += paragraph.length + 2`; a paragraph whose start offset < cutoff is kept; otherwise removed if an opener matches. Result `kept.join("\n\n").trim()`.
- `htmlToTextWithLinks(html, baseUrl?)` (194-218): replace `<a\b[^>]*?href\s*=\s*["']([^"']+)["'][^>]*>([\s\S]*?)</a>` (gi): label = inner with tags → " ", whitespace collapsed, trimmed; empty label → " "; url resolved with `new URL(href, baseUrl)` (malformed left as written); `^(#|javascript:|mailto:)` → ` label `; else ` [label](url) `; then htmlToText.

### 17.2 normalize.ts
- `US_STATES` full-name → 2-letter map incl. "district of columbia": DC (16-30).
- `METROS`: only `bay-area` with 44 names (39-54): san francisco, sf, south san francisco, oakland, berkeley, emeryville, alameda, san mateo, burlingame, redwood city, menlo park, palo alto, east palo alto, mountain view, sunnyvale, santa clara, san jose, cupertino, milpitas, fremont, hayward, san bruno, daly city, foster city, brisbane, los altos, campbell, los gatos, pleasanton, walnut creek, san rafael, novato, sausalito, richmond, bay area, sf bay area, san francisco bay area, silicon valley, peninsula, south bay, east bay, north bay.
- `REMOTE_WORDS = /\b(?:fully\s+remote|remote(?:\s*-?\s*(?:first|friendly|ok|eligible|us|usa|global|anywhere))?|work\s+from\s+home|wfh|distributed|virtual)\b/i`; `HYBRID_WORDS = /\b(?:hybrid|flexible\s+(?:work|location)|partially\s+remote)\b/i`; `ONSITE_WORDS = /\b(?:on-?site|in-?office|in-?person)\b/i` (62-65).
- `detectRemote({location?, title?, isRemoteFlag?, description?})` (75-98): flag true → remote; `strong = "<location> <title>"`: hybrid > remote > onsite; `location && parseLocation(location).city` → onsite; else description first 600 chars: hybrid > remote; else unknown.
- `LOCATION_NOISE = /\s*[-–—(]\s*(?:remote|hybrid|on-?site|in-?office|flexible|multiple locations|various)[^)]*\)?\s*$/i` (101-102).
- `parseLocation(raw)` (104-160): strip noise, trim; empty → all null except raw. lower in metro map → `{metro}` only. Split on `/\s*[,|/]\s*/`; city = parts[0]; for rest: US state full name → region code, country ??= US; `^[A-Z]{2}$` and ≠ "UK" → region, country ??= US; `^(?:usa|u\.s\.a?\.?|united states)$`i → country US; `^(?:uk|united kingdom|england)$`i → GB; else first unknown → region, later → country. metro = map[city.lower] ?? map[whole.lower] ?? null.
- `primaryLocation(locations)` (163-169): null if empty; first parsed with a metro, else first parsed.
- `CURRENCIES` (171-176): `$`,`us$`,`usd` → USD; `£`,`gbp` → GBP; `€`,`eur` → EUR; `c$`,`cad` → CAD.
- `parseAmount(text)` (179-190): `^([\d,.]+)\s*([km])?$`i on trimmed; strip commas; `Number(...)` not finite → null; k ×1e3, m ×1e6.
- `PERIOD_WORDS` (192-196): hour `\b(?:per\s+hour|hourly|\/\s*(?:hr|hour)|an\s+hour)\b`; month `\b(?:per\s+month|monthly|\/\s*(?:mo|month)|a\s+month)\b`; year `\b(?:per\s+year|annually|annual|\/\s*(?:yr|year)|a\s+year)\b` (all i; first match in that order wins).
- `parseSalary(text)` (205-262): first match of `(?<currency>US\$|C\$|[$£€])?\s*(?<min>[\d][\d,.]*\s*[km]?)\s*(?:-|–|—|to)\s*(?<currency2>US\$|C\$|[$£€])?\s*(?<max>[\d][\d,.]*\s*[km]?)` (i). Parse both; if `max >= 1000 && min < 1000 && /[km]/i.test(max text)` → `min *= (max >= 1e6 ? 1e6 : 1e3)`. `min <= 0 || min > max` → empty. `nearby = text.slice(max(0, index-40), index+120)`; currency = CURRENCIES[symbol lower] ?? CURRENCIES[code from /\b(USD|GBP|EUR|CAD)\b/i in nearby lower] ?? null; period word from nearby; `moneyWord = /\b(?:salary|salaries|compensation|comp|pay|paid|wage|base|ote|remuneration|stipend)\b/i`. No currency, no period word and no money word → empty ("Two numbers with a dash between them are usually not money"). period = word ?? (max < 1000 ? hour : year). Must pass `isPlausibleSalary`. Only the **first** range in the text is considered.
- `isPlausibleSalary({min,max,period})` (273-290): both non-null, `0 < min <= max`; hour: min ≥ 5 and max ≤ 2000; month: min ≥ 500 and max ≤ 200,000; year or null: min ≥ 15,000 and max ≤ 10,000,000.

### 17.3 dedupe.ts
- `COMPANY_SUFFIX = /\b(?:inc|inc\.|llc|l\.l\.c\.|ltd|ltd\.|limited|corp|corp\.|corporation|co|co\.|gmbh|sa|s\.a\.|plc|pty|ag|nv|bv)\b/gi` (14-15).
- `TITLE_NOISE` (23-29): `\([^)]*\)`, `\[[^\]]*\]`, `\bREQ[-\s#]?\d+\b` (gi), `\b(?:#\s*)?\d{4,}\b` (g), `\s+[-–—]\s+(?:remote|hybrid|on-?site|contract|full[-\s]?time|part[-\s]?time)\b.*$` (i) — each replaced by " " applied in order on the lowercased title.
- `normalizeCompany(name)` (31-38): lower; `&` → " and "; suffixes → " "; `[^a-z0-9]+` → " "; trim.
- `normalizeTitle(title)` (40-48): lower; noise → " "; `[^a-z0-9+#]+` → " "; trim (keeps C++, C#; keeps II/III).
- `normalizeLocationForKey(loc)` (50-56): null → ""; lower; `[^a-z0-9]+` → " "; trim.
- `dedupeKey({companyName, title, primaryLocation?})` (64-75) = `sha1_hex(normalizeCompany | normalizeTitle | normalizeLocationForKey)` joined with "|", UTF-8.
- `contentHash({title, locations, descriptionRaw?, salaryMin?, salaryMax?})` (83-101) = `sha1_hex(JSON.stringify([title, [...locations].sort(), descriptionRaw ?? "", salaryMin ?? null, salaryMax ?? null]))`. Keyed on what a user would notice, not fetched_at.
- `jobId(source, slug, externalId)` = `"<source>:<slug>:<externalId>"`; `webJobId(slug, url)` = `jobId("web", slug, sha1_hex(url))` (103-119). Invariant 2.

### 17.4 ingest.ts — `ingestJobs({ company?, jobs, deactivateMissing? }) -> { inserted, updated, unchanged, deactivated }` (28-164)
`BATCH_SIZE = 200` (26) ("Postgres limits a statement's parameters"). `now = new Date()` once per call.
Per batch:
1. `SELECT id, content_hash FROM jobs WHERE id IN (batch ids)`.
2. `changed` = jobs whose stored hash ≠ new hash (includes new ids); `identical` = known and same hash.
3. identical → `UPDATE jobs SET last_seen_at=now, fetched_at=now WHERE id IN (...)`; `unchanged += n`. (**Does not reset `is_active`** — an identical posting that was previously deactivated stays inactive; see hazards.)
4. changed → `INSERT INTO jobs (id, source, external_id, company_id = company?.id ?? null, company_name, title, locations, location_norm, remote, employment_type, salary_min = String(min)|null, salary_max, salary_currency, salary_period, description_raw, description_text, url, apply_url, posted_at, raw, content_hash, dedupe_key, is_active=true, first_seen_at=now, last_seen_at=now, fetched_at=now) ON CONFLICT (id) DO UPDATE SET company_id, company_name, title, locations, location_norm, remote, employment_type, salary_min, salary_max, salary_currency, salary_period, description_raw, description_text, url, apply_url, posted_at, raw, content_hash, dedupe_key = excluded.*, is_active=true, last_seen_at=now, fetched_at=now` — **first_seen_at deliberately not updated**. `inserted += new ids`, `updated += rest`.
After batches, if `deactivateMissing && company`: `UPDATE jobs SET is_active=false, fetched_at=now WHERE company_id = $company AND is_active AND id NOT IN (all fetched ids) RETURNING id` (if no ids fetched, every active job of the company is deactivated). Only for full board fetches ("never for an aggregator search, which only ever sees a slice") — invariant 7.
Never touches `canonical_job_id` (no code writes it; prefilter filters `canonical_job_id IS NULL`).

### 17.5 JSearch aggregator (sources/jsearch.ts)
- `HOST = "jsearch.p.rapidapi.com"`, endpoint `https://jsearch.p.rapidapi.com/search-v2` ("`/search` returns 'Endpoint does not exist', and paging is by an opaque `cursor`", 25-27).
- `MONTHLY_BUDGET = env.JSEARCH_MONTHLY_BUDGET ?? 180` (34); env default 180 (env.ts:73); free tier is 200/month.
- Errors: `JSearchDisabledError(reason)` "JSearch unavailable: <reason>" (36-41); `JSearchQuotaError(used, budget)` "JSearch monthly budget spent (<used>/<budget> requests)" (43-51).
- `searchJSearch({ query, country?='us', datePosted?='month', remoteOnly?, maxRequests?=1 })` (239-325):
  1. `!JSEARCH_ENABLED` → Disabled "JSEARCH_ENABLED is false"; `!RAPIDAPI_KEY` → Disabled "RAPIDAPI_KEY is not set".
  2. `budgetBefore = requestsThisPeriod('jsearch')`; `>= budget` → QuotaError.
  3. `maxRequests = max(1, min(maxRequests ?? 1, budget - budgetBefore))`.
  4. Loop pages: query params `query`, `country`, `date_posted`, `work_from_home=true` if remoteOnly, `cursor` if any. **Count before reading** (`countRequest('jsearch')` → returns month total; "a request that was sent and then failed still spent quota"). `fetchText(url, {timeoutMs:45_000, retries:1, accept, headers {x-rapidapi-key, x-rapidapi-host}})`. HttpError with jobs already collected → break (return partial); otherwise rethrow. Parse `{ status?, data?: { jobs?, cursor? } }`; normalize each job; record ATS board from apply links (dedupe by `source:slug`, companyName = employer_name.trim() || slug). Next `cursor = data.cursor ?? null`; stop if no cursor or page empty.
  5. Return `{ jobs, discoveredBoards, requestsUsed, requestsThisMonth }`.
- `atsFromApplyOptions(job)` (112-136): links = [job_apply_link, ...apply_options[].apply_link] truthy; first match of `/(?:boards|job-boards)\.greenhouse\.io\/([a-z0-9_-]+)/i`, `/jobs\.lever\.co\/([a-z0-9_-]+)/i`, `/jobs\.ashbyhq\.com\/([a-z0-9_-]+)/i` (per link, greenhouse then lever then ashby); slug lowercased.
- `employerSlug(name)` (139-147): lower, `[^a-z0-9]+` → "-", trim leading/trailing "-", slice(0,60), empty → "unknown".
- `normalizeJSearchJob(job)` (149-224): companyName = `employer_name?.trim() || "Unknown"`; slug = employerSlug; locations = `[job_location ?? [city,state,country].filter(Boolean).join(", ")].filter(Boolean)` (note: `job_location: ""` gives `[]` without falling back); remote = `job_is_remote === true ? 'remote' : detectRemote({location, title, description: cleaned})`; salary `{min: job_min_salary ?? null, max: job_max_salary ?? null, currency: 'USD', period: periodOf(job_salary_period)}` (`hour`/`month`/`year` or `annum` substring, lowercased) kept only if plausible else all-null; `applyLink = apply_options.find(is_direct)?.apply_link ?? job_apply_link ?? null`; `url = job_google_link ?? applyLink ?? job_apply_link ?? ""`; `employmentType = job_employment_type ?? null`; postedAt from `job_posted_at_datetime_utc`; id `jsearch:<employerSlug>:<job_id>`; description plain text through cleanDescription.
- `jsearchBudget()` (328-336) → `{ used, budget, remaining: max(budget-used,0), enabled: JSEARCH_ENABLED && Boolean(RAPIDAPI_KEY) }` (settings page).
- Usage queries (queries/usage.ts): `currentPeriod(at)` = UTC `YYYY-MM`; `countRequest(provider, count=1)` = `INSERT INTO api_usage (id='<provider>:<period>', provider, period, requests=count) ON CONFLICT (id) DO UPDATE SET requests = requests + count, updated_at = now RETURNING requests` (single upsert so concurrent fetchers cannot both read 199); `requestsThisPeriod(provider)` = select requests for (provider, period) or 0.

### 17.6 Careers-page LLM extractor (sources/careers-page.ts)
Rules (13-30): runs only where no API exists; URLs always real links from the page; unknown fields null; capped; a page that yields nothing is flagged `needs_browser`.
Constants: `MAX_LISTING_PAGES = 5`, `MAX_POSTINGS = 50`, `MAX_LISTING_CHARS = 30_000`, `MAX_POSTING_CHARS = 20_000` (33-39).
Zod schemas (all fields required, absence = null — "OpenAI's strict structured output rejects optional properties outright", 41-44):
- `ExtractedPosting = { title: string, location: string|null, url: string|null, team: string|null }` (45-55)
- `ExtractedListing = { postings: ExtractedPosting[], more_listing_urls: string[] }` (57-64)
Prompt: `LISTING_INSTRUCTIONS` at careers-page.ts:66-80; user prompt `"Careers page for <company.name> at <url>.\n\n<page>\n<text.slice(0, 30000)>\n</page>"` (158-160); LLM purpose `"extract"` via `generateStructured` (one retry on malformed output), context `options.context ?? {}`.
`fetchCareersPage(company: {name, slug, careersUrls}, { context?, knownContentHashes? (declared, unused), maxPostings?=50 })` (125-251):
1. BFS queue = careersUrls; while queue and pagesFetched < 5: shift url; skip seen; `fetchPage(url)` (text = htmlToTextWithLinks(html, url); any error → null → skip without counting); pagesFetched++; LLM extract.
2. For each posting: `resolved = resolveUrl(posting.url, pageUrl)` (absolute; http/https only; WHATWG `new URL().toString()`); skip null/duplicates; stop adding when `postings.size >= maxPostings` (break this page's loop). Push `more_listing_urls` (resolved, unseen) to queue.
3. For each posting (sequential): `id = webJobId(company.slug, url)`; `fetchPage(url)` (postingsFetched++ if ok); `cleaned = cleanDescription(page?.html ?? null)`; `text = cleaned.text.slice(0, 20000)`; `locations = posting.location ? [location] : []`; `salary = parseSalary(text)`; `contentHash({title, locations, descriptionRaw: text, salaryMin, salaryMax})`.
   NormalizedJob: `source 'web'`, `externalId = id.split(":").at(-1)` (the sha1), `companySlug = company.slug`, `companyName = company.name`, `remote = detectRemote({location, title, description: text})`, `employmentType null`, **`descriptionRaw = text || null` and `descriptionText = text || null`** (the raw HTML is not stored), `applyUrl null`, `postedAt null` ("a guessed date would wrongly age a job out of the search window"), `raw = { source: 'careers-page', team, listing: posting }`, dedupeKey from company.name/title/primary location.
4. Return `{ jobs, needsBrowser: pagesFetched > 0 && postings.size === 0, pagesFetched, postingsFetched }`.

---

## 18. Companies (registry, discovery, refresh)

### 18.1 Company queries (queries/companies.ts) — NOT user-scoped (shared mirror)
- `listCompanies({status?, limit=500})` ordered by name asc.
- `getCompanyBySlug(slug)`, `getCompanyByBoard(sourceType, atsSlug)`.
- `upsertCompany(input: NewCompany) -> Company` (61-91): existing = (atsSlug ? byBoard(sourceType, atsSlug) : null) ?? bySlug(slug). Existing → `UPDATE SET name = input.name, domain = input.domain ?? existing.domain, careers_urls = input.careersUrls ?? existing.careersUrls, tags = input.tags?.length ? union(existing, input) : existing, status = input.status ?? existing.status ("A company that was marked invalid and now verifies is active again"), needs_browser = input.needsBrowser ?? existing.needsBrowser, updated_at` (sourceType, atsSlug, slug, discoveredVia, addedByUserId never change). Else insert.
- `staleCompanies(staleBefore, limit=200)` (94-109).
- `recordFetch({companyId, status, jobCount, markInvalid?})` (111-127): `SET last_fetched_at=now, last_fetch_status, last_fetch_job_count, [status='invalid'], updated_at`.
- `findCompaniesByNames(names)` (164-199): per name: `key = lower.replace(/[^a-z0-9]+/g,"")`, `domain = trimmed lower minus http(s):// and www., up to first "/"`; skip empty key; match active companies where `regexp_replace(lower(name),'[^a-z0-9]+','','g') = key` OR same on slug OR on `lower(coalesce(ats_slug,''))` OR (domain contains "." and `domain = $domain`); order boards (greenhouse/lever/ashby) before others; limit 1. Returns `{found: [{name, company}], missing: names}`.
- Also `setCompanyStatus`, `countByStatus`, `knownSlugs` (UI/scripts).

### 18.2 Verification (companies/verify.ts)
- `VerifyOutcome = ok{source,slug,jobCount} | empty | not-found | error{error}` (14-18).
- `verifyBoard(source, slug)` (20-40): fetchBoard; jobs>0 → ok; 0 → empty; BoardNotFoundError → not-found; other → error.
- `findBoard(slug, sources = [greenhouse, lever, ashby])` (49-62): verify all **in parallel**; first ok (in source order) else first empty else null.
- `slugCandidates(name)` (71-97): base = trim, lower, remove `['’]` (ASCII apostrophe and U+2019), `&` → "and", remove `\b(?:inc|llc|ltd|corp|corporation|co|gmbh|plc)\.?\b` → " ", trim; words = split `/[^a-z0-9]+/` non-empty; none → []. Candidates (deduped, order kept, length ≥ 2): joined, hyphenated, words[0], joined+"inc", joined+"hq", joined+"jobs", joined+"careers".

### 18.3 Discovery (companies/discovery.ts)
Governing rule: "a company is registered only after a live API call returns postings" (12-25). Order: ATS link on the careers page → slug guesses → web search.
- `ATS_PATTERNS` (28-39, global regexes, lastIndex reset per call): greenhouse `/(?:boards|job-boards)\.greenhouse\.io\/(?:embed\/job_board\?for=)?([a-z0-9_-]+)/gi`; lever `/jobs\.lever\.co\/([a-z0-9_-]+)/gi`; ashby `/jobs\.ashbyhq\.com\/([a-z0-9_-]+)/gi`.
- `UNSUPPORTED_ATS` (46-47): `myworkdayjobs.com|icims.com|taleo.net|successfactors.com|smartrecruiters.com|workable.com|breezy.hr|jazzhr.com|bamboohr.com|jobs.gem.com|jobvite.com|recruitee.com|teamtailor.com|paylocity.com|dover.com|rippling(ats)?.com|pinpointhq.com|applytojob.com` (i).
- `findAtsLinks(html)` (52-69): all matches per pattern in pattern order; slug lowercased; skip `embed`/`job_board`; dedupe (source, slug).
- `hasUnsupportedAts(html)` (71-73).
- `normalizeDomain(input)` (76-86): trim lower; prepend `https://` if no `://`; WHATWG URL hostname minus leading `www.`; invalid → null.
- `CAREERS_PATHS = ["/careers", "/jobs", "/careers/jobs", "/company/careers"]` (89).
- `CAREERS_LINK = /href\s*=\s*["']([^"']*(?:career|jobs|join-us|work-with-us)[^"']*)["']/gi` (92-93); `findCareersLinks(html, base)` (102-119): resolve each, http(s) only, dedupe, first 5.
- `tryFetch(url)` (121-133): fetchText html accept, retries 1, 12 s; any error → null.
- `searchForCareersPage(name)` (155-181): only if `SEARCH_API_KEY`; GET `https://api.search.brave.com/res/v1/web/search?q=<name> careers jobs&count=5` headers `X-Subscription-Token`, `Accept: application/json`, 12 s timeout; non-ok/any error → []; returns `web.results[].url`.
- `DiscoveryResult = { name, registered: Company|null, source: board|'careers_page'|null, slug|null, careersUrls: string[], needsBrowser: boolean, trail: {step, detail}[] }` (135-147).
- `discoverCompany({ nameOrUrl, addedByUserId?, discoveredVia?, register?, tags? })` (189-393):
  0. `raw = trim`; `looksLikeUrl = /^(?:https?:\/\/|www\.)|\.[a-z]{2,}(?:\/|$)/i`; `domain = looksLikeUrl ? normalizeDomain(raw) : null`; `name = raw`; `careersUrls = []`.
  1. Pasted board link: `findAtsLinks(raw)[0]` → trail `ats-link` "pasted <source>:<slug>"; verifyBoard ok|empty → finish(board).
  2. If domain: `name = domain.split(".")[0]`; trail `domain`.
  3. Careers paths (only with domain): for each `https://<domain><path>`: tryFetch; skip failures; first page that loads: push url, find ATS links, trail `careers-page` "<url> → <links or 'no ATS link'>"; verify each link (ok|empty → finish); if unsupported ATS → trail `unsupported-ats` "Workday/iCIMS-style board — registering as a careers page" → finish(careers_page); then **break** (only the first loaded path is examined).
  4. Homepage (domain and no careers url yet): tryFetch `https://<domain>`; `findCareersLinks`; trail `homepage` "<n> careers links"; for each link: tryFetch, skip failures, push url, verify ATS links (ok|empty → trail `homepage-ats-link` → finish), unsupported → trail `unsupported-ats` <url> → finish(careers_page); break after first loaded.
  5. Slug guesses: for candidate in `slugCandidates(name)`: `findBoard(candidate)`; status ok (not empty) → trail `slug-guess` "<source>:<candidate>" → finish. Else trail `slug-guess` "no board for <first 3 candidates>".
  6. Search (only without domain): `searchForCareersPage(name)`; trail `search` "<n> results"; first 3 urls: tryFetch, push url, ATS links verify (ok|empty → trail `search-ats-link`, `domain ??= normalizeDomain(url)`, finish); unsupported → `domain ??= …`, trail, finish(careers_page). (No break here.)
  7. Any `careersUrls` → trail `careers-page-fallback` "no recognised ATS, registering the careers page for extraction" → finish(careers_page).
  8. Else return unregistered result.
  - `finish({source, slug})` (356-392): `ourSlug = slug ?? normalizeDomain(domain ?? name)?.split(".")[0] ?? name`; result with `needsBrowser = source === 'careers_page'`; if `register === false` return unregistered; else `upsertCompany({ name: titleCase(name), domain, slug: ourSlug.toLowerCase(), sourceType: source, atsSlug: slug, careersUrls, tags: tags ?? [], status: 'active', needsBrowser: source==='careers_page', discoveredVia: discoveredVia ?? 'agent', addedByUserId: addedByUserId ?? null })`. `titleCase` uppercases first char of each whitespace-separated word (395-400).
- `registerBoardsFromApplyLinks(boards, { addedByUserId? })` (411-445): for each board: skip if `getCompanyByBoard` exists; `verifyBoard` must be `ok` (empty not accepted); `upsertCompany({ name: companyName, slug: board.slug, domain: null, sourceType, atsSlug: slug, careersUrls: [], tags: [], status: 'active', discoveredVia: 'jsearch', addedByUserId })`.
- Callers: worker `company.discover` (via 'agent'), search run `resolveTargets` (via 'user'), and **inline in Next**: `addCompanyAction` (actions/companies.ts:46) and chat tool `add_company` (agents/tools/companies.ts:30) — both block a request for tens of seconds; `addCompanyAction` also calls `refreshCompany` inline for API boards.

### 18.4 Refresh (companies/refresh.ts)
- `BOARD_STALE_AFTER_MS = 2*60*60*1000`; `staleCutoff(now=Date.now()) = new Date(now - 2h)` (24-28). Shared by worker cron, search run, and "Refresh stale boards" button.
- `RefreshOutcome = { company: name, status: 'ok'|'not-found'|'unsupported'|'error', jobs, ingest?, error? }` (30-36).
- `refreshCompany(company)` (38-94): `careers_page` → `refreshCareersPage`. No adapter or no atsSlug → `unsupported` (no recordFetch). Else `fetchBoard(atsSlug)` → `ingestJobs({company, jobs, deactivateMissing: true})` → `recordFetch(ok, jobCount)` → ok outcome. Error: `gone = BoardNotFoundError` → `recordFetch(gone ? 'not-found' : 'error', 0, markInvalid: gone)` ("Only a 404 is conclusive. A timeout should not retire a company").
- `refreshCompanies(companies, concurrency = 6)` (97-115): worker pool, results in input order.
- `refreshCareersPage(company)` (125-173): no careersUrls → unsupported. `fetchCareersPage(company, { context: { userId: company.addedByUserId } })` → `ingestJobs({company, jobs})` (**no deactivation**: "an extraction that reads fewer roles than last time is more likely to be a page that changed shape") → `recordFetch(needsBrowser ? 'needs-browser' : 'ok', jobs.length)` → if `needsBrowser !== company.needsBrowser` update `companies.needs_browser` (set and cleared). Error → `recordFetch('error', 0)` (never invalid).
- `last_fetch_status` values written: `ok`, `not-found`, `error`, `needs-browser`.

### 18.5 Seed data (companies/seed.json) and scripts
- JSON array of 225 objects, all with exactly `{ name, slug, sourceType, atsSlug, domain, tags }`; sourceType counts: greenhouse 125, ashby 90, lever 10 (no careers_page); every entry has a domain; tags from {ai, enterprise, devtools, consumer, fintech, data, infra, marketplace, security, hardware, healthtech, climate, crypto, biotech}.
- `scripts/seed-companies.ts`: `upsertCompany({ name, slug, domain ?? null, sourceType, atsSlug, careersUrls: [], tags ?? [], status:'active', discoveredVia:'seed' })` per entry; prints counts by status; `--fetch` → `refreshCompanies(listCompanies({status:'active'}))` and prints totals.
- `scripts/fetch-board.ts <source> <slug> [--json] [--limit n=5]`: prints adapter output; exit 1 on BoardNotFound, 2 on usage.
- `scripts/verify-boards.ts --source s --slugs a,b | --guess Name… | --file list.json [--json]`: concurrency 6; prints verify/guess results.

---

## 19. Event kind catalogue (everything this area emits)

| kind | level | runId | approvalId | data | emitter |
|---|---|---|---|---|---|
| worker.ping | info | – | – | {jobId, workerPid} | handlers/ping.ts:14 |
| run.started | info | ✓ | – | – | search-run.ts:150; evaluate-run.ts:595 |
| search.criteria | info | ✓ | – | {brief, criteria} | search-run.ts:182 |
| search.companies.added | info | ✓ | – | – | search-run.ts:515 |
| search.companies.missing | warn | ✓ | – | – | search-run.ts:523 |
| source.refresh.start | info | ✓ | – | – | search-run.ts:216 |
| source.refresh.done | info | ✓ | – | – | search-run.ts:232 |
| source.aggregator.done | info | ✓ | – | {requestsThisMonth} | search-run.ts:604 |
| source.aggregator.skipped | info/warn | ✓ | – | – | search-run.ts:619 |
| prefilter.done | info | ✓ | – | {activeInMirror, selected, cappedAt} | search-run.ts:265 |
| scoring.start | info | ✓ | – | – | search-run.ts:295 |
| scoring.progress | debug | ✓ | – | – | search-run.ts:314 |
| scoring.done | info | ✓ | – | – | search-run.ts:331 |
| evaluation.skipped | warn/info | ✓ | sometimes | – | search-run.ts:389,407; evaluate-run.ts:255,278 |
| approval.requested * | info | ✓ | ✓ | {kind, count, estimate?} | approvals.ts:226 |
| approval.auto | info | ✓ | ✓ | {kind, count, estimate?} | approvals.ts:173 |
| approval.decided | info | ✓ | ✓ | {kind, via, approved, itemIds\|null} | approvals.ts:324 |
| approval.reopened | warn | ✓ | ✓ | – | approvals.ts:347 |
| approval.expired | warn | ✓ | ✓ | – | approvals.ts:567 |
| evaluation.start | info | ✓ | – | – | evaluate-run.ts:304 |
| evaluation.progress | debug | ✓ | – | – | evaluate-run.ts:692 |
| evaluation.failed | warn | ✓ | – | {failed:[{matchId,error}]} | evaluate-run.ts:334 |
| evaluation.done | info | ✓ | – | – | evaluate-run.ts:356 |
| shortlist.skipped | info | ✓ | ✓ | – | evaluate-run.ts:455 |
| shortlist.done | info | ✓ | – | {matchIds} | evaluate-run.ts:480 |
| run.completed * | info | ✓ | – | SearchRunOutput / MatchEvaluateOutput | evaluate-run.ts:137,640 |
| run.failed * | error | ✓ | – | – | evaluate-run.ts:215; runs.ts:238 (reaper) |
| run.cancelled | warn | ✓ | – | – | runs.ts:130; search-run.ts:465; evaluate-run.ts:795; tailor-run.ts:248 |
| tailor.started | info | ✓ | – | {matchId} | tailor-run.ts:99 |
| tailor.verified | info/warn | ✓ | – | {resumeId, checked, modelChecked, revised, unsupported, passed} | tailor-run.ts:178 |
| tailor.failed * | error | ✓ | – | – | tailor-run.ts:212 |
| tailor.done * | info/warn | ✓ | – | {matchId, resumeId, documentId} | handlers/resume.ts:75 |
| render.done | info | ✓ | – | {resumeId, documentId, pages} | handlers/resume.ts:111 |
| render.failed | warn (tailor) / error (render) | ✓ | – | {resumeId} | handlers/resume.ts:52,122 |
| company.discovered | info | null | – | {trail} | handlers/company.ts:32 |
| company.not-found | warn | null | – | {trail} | handlers/company.ts:32 |
| company.refresh.done | info | null | – | {boards, ok, notFound, errored} | handlers/company.ts:79 (only if userId) |
| chat.followup | debug | – (runId in data) | – | {conversationId, runId, messageId?, label?, skipped?, failed?} | chat/follow-up.ts:232 |
`*` = FOLLOW_UP_KINDS (queues `chat.follow_up` when the run has a conversation).
Client listeners (by kind): live-provider.tsx (approval.requested, company.discovered, render.done, run.completed, run.failed, tailor.done), worker-panel.tsx (worker.ping), companies page / refresh-stale-button (company.refresh.done, company.discovered), home/matches/activity/profile pages via `<RefreshOn>` (run.*, approval.*, tailor.done, render.done).

---

## 20. Constants (one table)

| Constant | Value | Where |
|---|---|---|
| heavy queue concurrency | clamp(WORKER_CONCURRENCY=4, 1, 4) | main.ts:48 |
| heartbeat interval | 10 s | main.ts:131 |
| worker stale after | 45 s | heartbeat.ts:18 |
| graceful stop timeout | 30 s | main.ts:146 |
| cron | `0 */6 * * *`, key `company-refresh-cron`, missed once | main.ts:119-122 |
| board stale after | 2 h | refresh.ts:24 |
| staleCompanies limit | 200 | queries/companies.ts:94 |
| listCompanies limit | 500 | queries/companies.ts:28 |
| refresh pool | 6 | refresh.ts:99 |
| pooled default | 6 | pool.ts:11 |
| HTTP timeout / retries | 20 s / 3 | http.ts:13-14 |
| Retry-After cap | 60 s | http.ts:36 |
| backoff | 2^n·500 ms + 0..250 ms | http.ts:39 |
| HttpError body cap | 2000 chars | http.ts:88 |
| JSearch timeout / retries | 45 s / 1 | jsearch.ts:282-283 |
| JSearch monthly budget | 180 (env) | jsearch.ts:34 |
| JSearch requests per run | 3 | search-run.ts:85 |
| careers page timeout / retries | 15 s / 1 | careers-page.ts:87-89 |
| careers listing pages / postings | 5 / 50 | careers-page.ts:33-35 |
| careers listing / posting chars | 30,000 / 20,000 | careers-page.ts:37-39 |
| discovery fetch timeout / retries | 12 s / 1 | discovery.ts:125-126 |
| Brave search timeout / count | 12 s / 5, first 3 used | discovery.ts:161,168,304 |
| findCareersLinks max | 5 | discovery.ts:118 |
| ingest batch | 200 | ingest.ts:26 |
| upsertMatches batch | 500 | search-run.ts:644 |
| tail fraction | 0.3 | clean.ts:19 |
| description head for remote | 600 chars | normalize.ts:92 |
| salary nearby window | −40 / +120 chars | normalize.ts:234 |
| approval TTL | 7 days | approvals.ts:29 |
| live: recent runs window | 10 min | live.ts:69 |
| live: event page | 200 (since>0) / 60 (first) | live.ts:109,116 |
| live: runs / approvals | 20 / 20 | live.ts:131,143 |
| EVAL_CONCURRENCY | 5 | evaluate-run.ts:67 |
| PROGRESS_EVERY | 3 | evaluate-run.ts:69 |
| top matches in output | 5 | evaluate-run.ts:122 |
| tailor in-flight dedupe window | 15 min | tailor-run.ts:277 |
| PDF render timeout | 30 s | pdf.ts:27 |
| PDF close-when-idle bound | 25 s | pdf.ts:169 |
| follow-up reply wait / poll | 90 s / 1.5 s | follow-up.ts:60-61 |
| follow-up agent timeout | 90 s | follow-up.ts:141 |
| follow-up proposed roles | 10 | follow-up.ts:174 |
| storage filename tail | 100 chars | storage/index.ts:38 |
| DB pool max | 10 prod / 5 dev | db/index.ts:24 |
| (ranking, used by runs) fast-score batch / concurrency / desc chars / threshold | 40 / 16 / 4000 / 55 | fast-score.ts:28-39,152 |
| (ranking) prefilter limit / max age | 600 / 45 days | prefilter.ts:35,312 |
| (ranking) deep-eval desc chars / call timeout | 12,000 / 4 min | deep-eval.ts:39,45 |
| (settings defaults) defaultMode / scoreThreshold / deepEvalTopN | approve_each / 55 (0-100) / 15 (0-60) | settings.ts:18-28 |

## 21. Env vars used by this area (src/env.ts; `FOO=` counts as unset)
`DATABASE_URL` (required URL), `NODE_ENV` (pool size), `WORKER_CONCURRENCY` (int>0, default 4), `STORAGE_DIR` (default ./data), `PDF_BROWSER_CHANNEL` (optional), `RAPIDAPI_KEY`, `JSEARCH_ENABLED` ("true"/"false", default false), `JSEARCH_MONTHLY_BUDGET` (int>0, default 180), `SEARCH_API_KEY` (Brave), plus the LLM vars (`LLM_DEFAULT`, `LLM_EXTRACT`, `LLM_SCORE`, `LLM_EVALUATE`, `LLM_TAILOR`, `LLM_VERIFY`, provider keys, `LLM_RPM_*`) consumed through lib/llm. `SKIP_ENV_VALIDATION=1` at docker build.

---

## 22. Porting hazards and notes (for the Django design)

Queue / process
1. **Queue engine swap.** pg-boss gives: Postgres-backed jobs in schema `pgboss`, per-queue retryLimit + expireInSeconds (active-job expiry → retry/fail), LISTEN/NOTIFY wake-up, cron with `missed: once`, per-queue local concurrency, graceful drain. Python candidates that keep "Postgres only, no Redis": Procrastinate (Django integration, LISTEN/NOTIFY, retries, periodic tasks, queueing locks, per-worker concurrency per queue set) or a hand-rolled `SELECT … FOR UPDATE SKIP LOCKED` worker. Celery would add a broker. Per-queue concurrency means either one worker per queue group or an in-process semaphore per queue.
2. **In-flight state at cutover.** Pending approvals store `continuation = {"queue": "search.evaluate"|"search.shortlist", "payload": {...}}` verbatim; the Python side must accept those wire names (or migrate the JSON). Jobs still in `pgboss.job` at cutover are lost unless drained first.
3. **Reaper assumes a single worker.** `reapInterruptedRuns` fails every `running` run globally on boot. With several Python worker processes (or a rolling deploy) this would fail live runs. Needs a worker-instance id / lease if more than one worker runs. Also: an autopilot run is `running` while its continuation sits in the queue; a restart in that window fails it and the continuation then silently returns.
4. **singletonKey is a no-op** for `chat.follow_up` (standard policy); dedupe is `alreadyFollowedUp`. Port can add a real unique constraint.
5. **Retry semantics:** handler wrapper rethrows (main.ts:76-85), so `company.discover` (retryLimit 1) re-runs all names; `resume.render` (retryLimit 1) only retries errors outside its internal try. Expiry: a job active longer than expireInSeconds is considered dead by pg-boss supervision.
6. **Cooperative cancellation** relies on DB polling (`isCancelled`) between stages/batches/items; nothing is killed.
7. **Unconditional finishes**: `startRun` in `executeTailorRun`/`handleResumeRender` and `finishRun(completed)` in `handleResumeTailor`/`handleResumeRender` can overwrite a user's cancel (contrast `finishActiveRun`). Decide whether to preserve.
8. **Expired approvals only sweep on page load / chat** (no cron). A Django port may want a periodic sweeper, but the client relies on the `/api/live?since=0` sweep behaviour.
9. **Inline-in-request discovery**: `addCompanyAction`, chat `add_company` call `discoverCompany` (and `refreshCompany`) inside the request — tens of seconds. Keep or turn into jobs.
10. **Async-local LLM context** (`withLlmContext`) attributes ledger rows to user/run; Python needs `contextvars`.
11. **Shutdown with Playwright**: Python Playwright also installs signal handlers (`handle_sigint` etc. — set False); browser shared per process; use the async API if the worker is asyncio; one context per render; route-abort all requests; JS disabled.

Byte-for-byte compatibility (existing rows must keep matching)
12. **`content_hash`** = sha1 of `JSON.stringify([...])`. Python must use `json.dumps(…, separators=(",", ":"), ensure_ascii=False)` and render integral floats as ints (JS prints `120000`, Python `120000.0`); salary values from `parseSalary` multiplications and from Lever/Ashby JSON must be normalized. JS `sort()` orders by UTF-16 code units (differs from Python for astral characters). Mismatch makes every job look "changed" on the first Python refresh (mass rewrites but no data loss).
13. **`dedupe_key`/`webJobId`** must match: sha1 over UTF-8; `web:` ids are sha1 of the WHATWG-serialized URL from `new URL(candidate, base).toString()` (host lowercasing, default-port removal, percent-encoding, trailing "/" on bare origins). `urllib.parse.urljoin` differs; use a WHATWG URL implementation (e.g. `ada-url` bindings) or careers-page jobs get new ids and duplicate.
14. **Regex dialect**: JS `\b`, `\w`, `\d` are ASCII-only while JS `\s` is Unicode (incl. NBSP). Python `re` makes all of them Unicode by default and `re.ASCII` makes `\s` ASCII too — neither matches JS. Use explicit classes (`[A-Za-z0-9_]`, `[0-9]`, ASCII word-boundary lookarounds) or the `regex` module. Named groups `(?<n>)` → `(?P<n>)`. Global-regex `lastIndex` resets are a JS artefact.
15. **String lengths**: JS `.length`/`.slice` count UTF-16 units (TAIL_FRACTION cutoff, MAX_LISTING_CHARS, MAX_POSTING_CHARS, description head 600, nearby −40/+120). Careers-page `content_hash` uses the 20,000-unit slice.
16. **`??` vs `or`**: many fallbacks are nullish-only (Lever `allLocations: []`, JSearch `job_location: ""`, `upsertCompany` `careersUrls ?? existing` replaces with `[]`). Use `is None` checks, not truthiness. Conversely `posting.salaryRange?.min` is a truthiness check (0 → none) and `job.company_name?.trim() || slug` is truthiness.
17. **`encodeURIComponent`** → `quote(slug, safe="-_.!~*'()")`; URLSearchParams encodes spaces as `+`.
18. **HTTP client**: Node fetch follows redirects; httpx does not by default (`follow_redirects=True`). Keep the User-Agent string, per-attempt timeout, Retry-After handling, retryable set {408, 429, 5xx}.
19. **Dates**: `new Date(str)` accepts ISO with offsets and epoch ms (Lever). An invalid date would throw at insert time in TS (whole board refresh errors); decide Python behaviour explicitly.
20. **Numeric salaries** are stored as `numeric` via `String(number)`; read back with `::float8`.

Latent bugs worth a decision (preserve vs fix)
21. `ingestJobs` "identical" branch never sets `is_active = true`: a posting deactivated after a transient miss and seen again unchanged stays inactive forever (ingest.ts:67-73 vs comment at 131).
22. Duplicate ids inside one ingest batch (e.g. JSearch returning the same `job_id` on two pages) make Postgres raise "ON CONFLICT DO UPDATE command cannot affect row a second time"; in a search the aggregator step is then reported as `source.aggregator.skipped` warn. Dedupe by id per batch.
23. `upsertCompany` falls back to matching by our `slug`; discovery registers with `slug = ATS slug`, so a new board whose slug equals another company's slug silently updates that other company (name/domain/careersUrls) and returns it with the old source/atsSlug.
24. `careers-page` `knownContentHashes` option is declared and never used; every refresh re-reads every posting and re-pays the LLM for listings.
25. `runs.pgboss_job_id`, `runs.parent_run_id`, `jobs.canonical_job_id`, run kinds `company_discovery|company_refresh|profile_import` and approval kinds `tailor|company_add` exist in the schema but are never written.
26. `upsertMatches` reports `newMatches` as the count it tried to insert, not rows actually inserted (a concurrent insert would be over-counted).
27. `executeMatchEvaluate` re-evaluates matches that already have a deep evaluation (no `matchesNeedingEvaluation` filter) — intentional for on-demand "evaluate again"? It does pay again.

Test fixtures usable as Python golden tests: `src/test/fixtures/{greenhouse,lever,ashby,jsearch,careers-pages}/*` and unit tests `adapters.test.ts`, `clean.test.ts`, `normalize.test.ts`, `jsearch.test.ts`, `discovery.test.ts`, `careers-page.test.ts`, `approvals.test.ts`, `search-gates.test.ts`, `follow-up*.test.ts`. A parity harness should run the TS normalizers over the fixtures, dump `{id, contentHash, dedupeKey, descriptionText, salary, remote, locationNorm}` and assert the Python port matches.
