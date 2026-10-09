# Area map: tests, scripts, infra

Repo: `/Users/haydenbarolette/Projects/JobSearchAgent/.claude/worktrees/middleware-django-migration-389b1a`
(all paths below are relative to it). Read-only survey, 2026-10-04.

Scope: `src/test/**` (setup, 21 test files, fixtures), `vitest.config.ts`, `scripts/**` (7 scripts),
`.cx.ts`, `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `.claude/launch.json`, `next.config.ts`,
`src/env.ts`, `.env.example`, `src/lib/load-dotenv.ts`, `eslint.config.mjs`, `tsconfig.json`,
`package.json` scripts, `README.md`, `AGENTS.md`, minor config files, and a purity census of `src/lib`.

---

## 0. TL;DR for the Django port

* **301 Vitest cases in 21 files.** 20 files exercise server logic and become **pytest** tests;
  only `format.test.ts` exercises frontend-only code and stays a **frontend Vitest** test.
  (`interview.test.ts` is backend, but `SLOTS` is also consumed by a client component, so keep a
  small contract check on whatever copy the frontend ends up with.)
* **3 files need a live Postgres** (`approvals`, `follow-up-db`, `search-gates`): pytest-django with a
  real database (not SQLite: they rely on jsonb, `ON CONFLICT`/conditional updates, regex `\y`,
  concurrency). They fake the queue sender, never the DB.
* **2 files depend on the developer's `.env` LLM configuration** even though no model is called:
  `deep-eval.test.ts` (`runDeepEval` → `callLLM` → `modelRef("evaluate")` must resolve to a provider
  with a key) and `search-gates.test.ts` (asserts the estimate model is literally
  `"openai:gpt-5.6-terra"`, i.e. `LLM_EVALUATE` in `.env`). Make these explicit settings overrides in pytest.
* **`search-gates.test.ts` needs ≥35 active rows in `jobs`** in the dev DB (it borrows real postings;
  pool of 60) — i.e. `seed:companies --fetch` must have run. Provide a pytest fixture that inserts jobs.
* **7 scripts** → Django management commands (`dev_session`, `set_password`, `seed_companies`,
  `fetch_board`, `verify_boards`, `llm_smoke`, `llm_bakeoff`) plus `manage.py migrate` replacing
  `src/lib/db/migrate.ts`. `.cx.ts` is a tracked scratch file; do not port.
* **Topology today:** Postgres 18 (pgvector image) in Docker; Next (pages + API + server actions +
  chat streaming) and a tsx worker (pg-boss, Playwright PDF) run natively in dev; prod compose =
  db + one-shot migrate + web + worker sharing a `storage-data` volume.
  **Topology after:** db + migrate (`manage.py migrate`) + Django API (ASGI, streaming) + Python worker
  (queue + cron + heartbeat + Playwright/Chromium) + Next frontend-only, with same-origin routing of
  `/api/*` from the browser to Django (the auth client and cookies assume same origin).

---

## 1. Test harness

### 1.1 `vitest.config.ts` (13 lines)
* `resolve.alias`: `@` → `./src` (vitest.config.ts:5-7) — mirrors tsconfig `paths` `@/*` → `./src/*` (tsconfig.json:21-23).
* `test.environment: "node"` (:9), `include: ["src/test/**/*.test.ts"]` (:10), `setupFiles: ["./src/test/setup.ts"]` (:11).
* No `fileParallelism`/`pool` settings → Vitest default: **files run in parallel workers**, tests within
  a file sequentially. DB test files isolate themselves by unique user id, so this is safe.
* No coverage, no globalSetup/teardown, no DB transaction wrapping.
* Fixture paths are opened **relative to the process cwd** (`readFileSync("src/test/fixtures/...")`,
  e.g. adapters.test.ts:17-18) → tests must run from repo root. In pytest, resolve relative to `__file__`.
* Commands: `pnpm test` = `vitest run`; `pnpm test:watch` = `vitest` (package.json:22-23).

### 1.2 `src/test/setup.ts` (6 lines)
* Only `import "@/lib/load-dotenv"` (setup.ts:6). Reason (setup.ts:1-5): tests import modules that
  read `env`, so `.env` must load before `src/env.ts` validates; in CI the same vars are expected in
  the real environment.
* Consequence: **every test file that transitively imports `@/env` requires `DATABASE_URL` to be a
  valid URL** even if it never queries (e.g. `brief.test.ts` imports `ranking/prefilter` → `db` →
  `env`). `new Pool()` does not connect until first query, so pure tests do not need Postgres up.
* Note: the worktree itself has **no `.env`** (only the main checkout has one), so `pnpm test` in this
  worktree fails env validation unless vars are exported.

### 1.3 Conventions encoded in the tests (CLAUDE.md "Tests" section)
* DB tests "insert a throwaway user and delete it afterwards" — every per-user table cascades from
  `user`, so `DELETE FROM "user" WHERE id = ...` cleans up runs, approvals, agent_events, job_matches,
  resumes, conversations, messages (approvals.test.ts:26-30, 35-43).
  User ids: `test-approvals-<uuid>`, `test-follow-up-<uuid>`, `test-gates-<uuid>`; email `${id}@example.test`; name set.
* Queue is faked with `vi.mock("@/lib/queue/send", () => ({ send: vi.fn(async () => "job-id") }))`
  (approvals.test.ts:8, follow-up-db.test.ts:12, search-gates.test.ts:22). Rationale (approvals.test.ts:6-7):
  "a live worker would pick the continuation up and resume a run that belongs to a test". Assertions are
  made on `send` call args: `(queueName, payload, options?)`.
* Watch-mode worker shares the dev DB; its boot-time reaper (`reapInterruptedRuns`, runs.ts:225) can fail
  a test's `running` run mid-suite → re-run before chasing (CLAUDE.md "Tests").
* Model calls are never made: either a mock model (`MockLanguageModelV4` from `ai/test`,
  deep-eval.test.ts:1, 288-298), an injected judge function (`vi.fn<Judge>`, verify.test.ts:426), or a
  module mock replacing the LLM-backed function (search-gates.test.ts:24-77).

---

## 2. Per-file test inventory

Legend: **Target** = where the test should live after the port.
`pytest` = backend unit; `pytest+DB` = backend with Postgres; `vitest(FE)` = stays frontend.

| File | Cases | Module(s) under test | DB? | Env? | Fixtures | Mocks | Target |
|---|---|---|---|---|---|---|---|
| adapters.test.ts | 20 | `sources/greenhouse,lever,ashby` normalizers | no | no | greenhouse/anthropic.json, lever/matchgroup.json, ashby/linear.json, ashby/vanta.json | none | pytest |
| approvals.test.ts | 18 | `runs/approvals`, `runs/runs` | **yes** | DATABASE_URL | — | queue `send` | pytest+DB |
| brief.test.ts | 8 | `ranking/brief`, `ranking/prefilter.companyNamePattern` | no | DATABASE_URL (import) | — | none | pytest |
| careers-page.test.ts | 7 | `sources/clean.htmlToText/htmlToTextWithLinks` | no | no | careers-pages/retool.html, crusoe.html | none | pytest |
| clean.test.ts | 20 | `sources/clean`, `sources/dedupe` | no | no | — | none | pytest |
| deep-eval.test.ts | 15 | `ranking/deep-eval` (prompt, sanitize, estimate, run) | no | **LLM_EVALUATE/LLM_DEFAULT + key** for `runDeepEval` | — | `MockLanguageModelV4` | pytest (fake LLM) |
| discovery.test.ts | 20 | `companies/discovery` pure fns, `companies/verify.slugCandidates` | no | DATABASE_URL (import) | — | none | pytest |
| follow-up-db.test.ts | 5 | `chat/follow-up` (`replyInFlight`, `runChatFollowUp`), `runs/events.emit` | **yes** | DATABASE_URL | — | queue `send` | pytest+DB |
| follow-up.test.ts | 5 | `chat/follow-up.describeUpdate` | no | DATABASE_URL (import) | — | none | pytest |
| format.test.ts | 16 | `lib/format` | no | no | — | none | **vitest(FE)** |
| interview.test.ts | 10 | `profile/interview` | no | no | — | none | pytest (+FE contract for SLOTS) |
| jsearch.test.ts | 13 | `sources/jsearch` pure fns | no | DATABASE_URL (import) | jsearch/entry-level-sf.json | none | pytest |
| normalize.test.ts | 30 | `sources/normalize` | no | no | — | none | pytest |
| prefilter.test.ts | 15 | `ranking/prefilter` pure fns | no | DATABASE_URL (import) | — | none | pytest |
| pricing.test.ts | 10 | `llm/pricing` | no | no | — | none | pytest |
| resume-edits.test.ts | 8 | `resume/edits.stampUserEdits` | no | no | — | none | pytest |
| resume-template.test.ts | 16 | `resume/render/template` | no | no | — | none | pytest |
| search-gates.test.ts | 15 | `runs/search-run.gateDeepEvaluation`, `runs/evaluate-run`, `db/queries/matches`, approvals | **yes** (+≥35 active jobs) | **LLM_EVALUATE=openai:gpt-5.6-terra** | — | queue `send`, `ranking/deep-eval` (`loadEvalCandidate`, `evaluateMatch`) | pytest+DB |
| tailor.test.ts | 12 | `resume/tailor` pure fns | no | no | — | none | pytest |
| token-accounting.test.ts | 6 | `llm/ledger.tokensFromProviderUsage`, pricing | no | DATABASE_URL (import) | — | none | pytest (reshape for Python SDK usage objects) |
| verify.test.ts | 32 | `resume/verify` | no | no | — | `vi.fn<Judge>` | pytest |

### 2.1 adapters.test.ts — board adapters against real captured payloads
Functions: `normalizeGreenhouseJob(slug, job)` greenhouse.ts:79; `normalizeLeverJob(slug, posting)` lever.ts:90;
`normalizeAshbyJob(slug, job)` ashby.ts:98. Fixture JSON loaded with `JSON.parse(readFileSync(...))`.
* Greenhouse (`anthropic.json`, shape `{jobs: [...]}`):
  * id matches `/^greenhouse:anthropic:\d+$/`; `externalId` is a string (numeric id coerced) (:38-43).
  * `descriptionRaw === raw.content` exactly, and still contains `&lt;` (entity-escaped) (:45-50) — **invariant: never rewrite description_raw**.
  * `descriptionText` contains no `&lt;` nor `<div`, length > 200 (:52-57).
  * No location contains `|` (split on pipe) (:59-67).
  * Splits on `;` as well as `|`, de-duplicates; real value `"New York City, NY; San Francisco, CA | New York City, NY"` → contains both cities once, and `locationNorm.metro === "bay-area"` (:69-84).
  * Any SF job → metro `bay-area` (:86-91).
  * Every job: `postedAt` valid Date; `dedupeKey` and `contentHash` match `/^[0-9a-f]{40}$/` (sha1 hex) (:93-100).
* Lever (`matchgroup.json`, bare array):
  * `title === posting.text`; `id === "lever:matchgroup:" + posting.id` (UUID) (:108-111).
  * Posting with `lists` → `descriptionRaw` longer than `description` and contains every `list.text` (:113-127).
  * Structured `salaryRange` → `salary {min,max,currency}` (:129-139; skipped if fixture has none).
  * `remote === workplaceType.toLowerCase()` (:141-146).
  * `applyUrl !== url` and `applyUrl` contains `/apply` (:148-153).
  * `postedAt.getTime() === createdAt` (epoch ms) (:155-157).
* Ashby (`linear.json`, `vanta.json`, shape `{jobs: [...]}`):
  * `descriptionRaw === descriptionHtml`; text has no `<p` (:164-167).
  * Vanta posting whose `compensation.summaryComponents[].compensationType` is "salary" → `salary.min > 0`, currency `USD`, period `year` (:169-181).
  * Linear (no compensation published) still has `salary.min` key and id `^ashby:linear:` (:183-189).
  * `secondaryLocations` → more than one location (:191-197).
  * `applyUrl !== url` (:199-203).
* Across adapters: `descriptionText.length <= descriptionRaw.length` and text has no `<(p|div|li|ul|h[1-6])\b` (:206-223);
  id splits into 3 parts on `:`, `remote ∈ {remote,hybrid,onsite,unknown}`, url `^https?://` (:225-237).

### 2.2 approvals.test.ts — approval gate state machine (DB)
Setup: user insert/delete (:35-43); `sent.mockClear()` per test (:45-47).
Helpers: `batchPayload()` = 3 items `{matchId: uuid, jobId, title, company, fastScore}`, `estimate {usd:0.15, perItemUsd:0.05, model:"openai:gpt-5.6-terra"}`, `note:null` (:49-59);
`pausedAt(mode, payload)` = `createRun({userId, kind:"search", status:"running", mode})` then
`requestApproval({userId, runId, kind:"deep_eval_batch", title:"Deep-evaluate 3 roles (about $0.15)", payload, mode, continuation: (approvalId) => ({queue:"search.evaluate", payload:{userId, runId, approvalId}}), stats:{phase:"awaiting_evaluation", toEvaluate:3}})` (:61-80).
Assertions:
* approve_each: `status "pending"`, `decision null`, `expiresAt - now > APPROVAL_TTL_MS - 60s` (TTL = 7 days, approvals.ts:29); `continuation` stored as `{queue, payload:{..., approvalId}}`; run `status "awaiting_approval"`, `stats ⊇ {phase:"awaiting_evaluation", toEvaluate:3}`; events `["approval.requested"]` with `approvalId`; **nothing sent** (:96-117).
* autopilot: `status "auto_approved"`, `decidedVia "autopilot"`, `decision {approved:true, itemIds:[all matchIds in payload order]}`, `isApproved()` true; `send("search.evaluate", {userId, runId, approvalId})` exactly once; run stays `running`; events `["approval.auto"]` (:119-141).
* Run already `cancelled` → approval created as `expired`; `getApproval` returns expired; run stays cancelled (:143-161).
* `decideApproval(userId, id, {approved:true, itemIds:[third, "not-an-item", first]}, "ui")` → `{ok:true, approval}` with `status "approved"`, `decidedVia "ui"`, `decision {approved:true, itemIds:[first, third]}` (payload order, unknown dropped); `approvedItemIds()` same; continuation sent once; event `approval.decided` message `"Approved deep evaluation of 2 of 3 roles."` (:165-197).
* No subset → all items; `decidedVia "chat"` (:199-208).
* Second decision → `{ok:false, reason:"already_decided"}`; one send total; status stays approved (:210-220).
* **Concurrent** `Promise.all` of three decisions → exactly one `ok`, one send (:222-233) — requires an atomic conditional UPDATE (`... WHERE status='pending' RETURNING`).
* Approved empty subset (`itemIds:["nothing-here"]`) → `status "rejected"`, `decision {approved:false, itemIds:[]}`, still sends continuation (the continuation finishes a rejected run); event message `"Skipped the deep evaluation."` (:235-259).
* Lapsed (`expiresAt` set to past via direct UPDATE): `getApproval` reports `expired` before any sweep; `listPendingApprovals` excludes it; `decideApproval` → `{ok:false, reason:"expired"}`; row `status expired`, `decision null`; **no send after the sweep** (listPendingApprovals already swept and resumed); events include `approval.expired` (:261-285).
* Sweep resumes exactly once even if `listPendingApprovals` called twice: exactly one `["search.evaluate", {userId, runId, approvalId}]` (:287-303).
* Unknown uuid and malformed id `"approval-7"` → `{ok:false, reason:"not_found"}` (:305-314) — malformed ids must not raise (uuid cast).
* Other user → `not_found`, no send (:316-322).
* `requestCancel(userId, runId)` on paused run → run `cancelled`; approval `expired`; not pending; decide → `expired`; no send; events include `run.cancelled` (:326-342).
* `listPendingApprovals` lists only answerable ones (:344-352).
* `resolveDecision(payload, decision)` (pure, approvals.ts:365): items identified by `matchId` **or `id`**; keeps payload order, drops unknown & duplicate ids; `approved:true` without itemIds → all; `approved:false` → `itemIds: []`; empty approved subset → `{approved:false, itemIds:[]}`; payload without `items` (or null) → plain `{approved}` with no `itemIds` key (:355-389).

### 2.3 brief.test.ts — search brief and company-name matching
Functions: `applyBrief` brief.ts:114, `cleanBrief` :65, `describeBrief` :151, `EMPTY_BRIEF` :37, `isEmptyBrief` :109, `mergeBrief` :93, `parseBrief` :56, `requestText` :50, `SAVED_PREFERENCES_PROMPT = "Search with my saved preferences"` :47; `companyNamePattern` prefilter.ts:441.
* `applyBrief(saved, {...EMPTY_BRIEF, seniority:"entry", roles:["software engineer"], companies:["Anthropic"]})` overrides seniority/target_roles only; locations/remote_pref kept; input object not mutated (:26-39).
* Empty brief → equal to saved; `isEmptyBrief(EMPTY_BRIEF)` (:41-44).
* `mergeBrief(derived, hints)`: hints win field by field; an **empty list means "not stated"** so derived stands (:46-56).
* `cleanBrief({companies:[" Anthropic ", "anthropic", ""], focus:"  "})` → trimmed, case-insensitive dedupe keeping first spelling, blank focus → null (:58-62). `parseBrief({seniority:"wizard"})` and `parseBrief(null)` → `EMPTY_BRIEF` (junk = no brief) (:63-64).
* `describeBrief` exact strings: `"Entry-level software engineer roles at Anthropic, in San Francisco Bay Area"`; `"Roles at Stripe, Plaid or Ramp, remote only"`; focus-only → `null`; role already naming level not doubled → `"Senior Backend Engineer roles"` (:67-85).
* `requestText(SAVED_PREFERENCES_PROMPT)` → null; whitespace → null; trims (:87-91).
* `companyNamePattern(name)` returns a **Postgres regex** using `\y` word boundaries; test converts `\y`→`\b` (:95-96). Matches whole words at start: "Anthropic PBC", "Scale AI, Inc." yes; "Metabase" (for Meta), "Not Anthropic" no; `" - "` → null (:98-108).

### 2.4 careers-page.test.ts — keeping links through HTML→text
Why (file header :6-13): extractor must copy real posting links; `htmlToText` strips hrefs, which once made a page of real jobs extract as zero.
* `htmlToText('<a href="/careers/swe">Software Engineer</a>') === "Software Engineer"`; `htmlToTextWithLinks` contains `[Software Engineer](/careers/swe)` (:25-32).
* Absolutises against base URL: `[SWE](https://acme.com/careers/swe)` (:34-38).
* `#top` and `mailto:` anchors keep label, drop target (:40-50).
* Markup inside label stripped: `[Staff Engineer](/x)` (:52-56); link with no readable label (only `<img>`) dropped (:58-62).
* Real `retool.html` with base `https://retool.com/careers` contains `/\[[^\]]*Engineer[^\]]*\]\(https:\/\/retool\.com\/careers\//`; `htmlToText(retool)` has no such links (:64-73).
* Distinct job-ish links (`/job|career|position|opening/i`): crusoe (JS-rendered page) < 6, retool > 10 (:75-93) — the JS-rendered case must later flag `needs_browser`.

### 2.5 clean.test.ts — cleaning and dedupe
`htmlToText` clean.ts:111, `cleanDescription` clean.ts:141 → `{text, removed: string[]}`; dedupe.ts: `normalizeCompany` :31, `normalizeTitle` :40, `dedupeKey` :64, `contentHash` :83.
* Lists → `• First thing` bullets (:12-16); entities decoded incl. `&rsquo;` → `’` (`"Ben & Jerry’s"`) (:18-20); script/style content dropped (:22-27).
* `cleanDescription("&lt;p&gt;We are hiring an engineer.&lt;/p&gt;").text === "We are hiring an engineer."` (:31-35).
* Trailing EEO block stripped, `removed` contains `"equal opportunity"` (:37-46); E-Verify + accommodation tail → `removed ⊇ ["E-Verify","accommodation"]` (:48-57).
* Same wording at the **start** of the body is kept, `removed` empty (:59-68) — only the trailing portion is scanned (DESIGN invariant 5).
* Never summarises: every paragraph survives verbatim (:70-78); plain text passes through (:80-82); `null` → `{text:"", removed:[]}` (:84-86).
* `normalizeCompany("Stripe, Inc.") == normalizeCompany("Stripe")`, `"Acme Corp" == "ACME"` (:90-93).
* `normalizeTitle` strips `(Remote)`, `- Remote`, `[REQ-1234]` (:95-105); keeps `II` vs `III` distinct (:107-109); keeps `C++` vs `C#` distinct (:111-113).
* `dedupeKey({companyName, title, primaryLocation})`: aggregator vs board copies collapse (case-insensitive location) (:115-129); different titles or cities do not collapse (:131-143).
* `contentHash({title, locations, descriptionRaw, salaryMin, salaryMax})`: stable; location order-insensitive; changes with description or pay (:146-170).

### 2.6 deep-eval.test.ts — deep evaluation pure parts + one mocked run
Fixtures built in code: `ResumeSchema.parse(...)` with ids `work-1, edu-1, proj-1, skill-1` (:29-45); `EvalCandidate {resumeId, resumeVersion, resumeText: resumeToText(resume), preferences, factIds: Set(factIds(resume))}` (:47-57); `EvalPosting` (:59-71); a full `DeepEval` object (:73-97).
Functions (deep-eval.ts): `sanitizeDeepEval` :218, `stripFactIds` :267, `evalInstructions` :139, `evalPrompt(posting, now)` :152, `estimateEvalCost(n, {inputChars?, modelRef?, at?})` :453, `estimateEvalCostFor` :486, `costPhrase` :505, `runDeepEval` :283, constants `MAX_DESCRIPTION_CHARS = 12_000` :39, `EVAL_OUTPUT_TOKENS = 1_800` :432 (SCHEMA_OVERHEAD_TOKENS 400 :434, CHARS_PER_TOKEN 4 :436, POSTING_FRAME_CHARS 400).
* `sanitizeDeepEval(eval, factIds)`: keeps real ids, forgives `[skill-1]` brackets, drops invented and duplicates → `["work-1","skill-1"]`; all-invented → `[]` but evidence text kept (:100-108); nothing else changes (:110-116).
* Prose clean-up: bracketed known ids (`[uuid]`, `[uuid, uuid]`, `[work-1]`) are stripped from **every** prose field (summary, requirement, evidence, gap, note, risk_flags, talking_points) with whitespace/punctuation tidied (`"Strong backend fit. Payments is adjacent."`); ids cited only in evidence prose become `fact_ids` (`[id1, id2]`); a risk flag that was only an id is dropped (:124-161). Unknown bracketed uuid stripped but not cited (:163-176). Ordinary brackets `[Go]`, `[2021–2023]` untouched (:178-182).
* `evalInstructions(candidate)` contains `"[work-1] Backend Engineer — Initech"`, `"[edu-1]"`, `"target_roles"`, not the company name (stable/cacheable half) (:186-192).
* `evalPrompt(posting, 2026-09-24T12:00Z)` contains `"Title: Backend Engineer, Payments"`, `"Pay: 150,000–190,000 USD per year"`, `"Posted: 2026-09-10 (14 days ago)"`, `"Today: 2026-09-24"` (:194-200). No pay → `"Pay: unstated"`, no date → `"Posted: unknown"`, description cut at 12,000 chars followed by `"\n[Description cut at"` (:202-214).
* `estimateEvalCost(10, {inputChars:16000, modelRef:"openai:gpt-5.6-terra"})` = per item `(4400/1e6)*2 + (1800/1e6)*12`, ×10 (:218-231); linear, 0 for none (:233-238); unpriced model → `{usd:null, perItemUsd:null, model}` (:240-246); DeepSeek off-peak (18:00Z) / peak (10:00Z) ratio 0.5 (:248-259); `estimateEvalCostFor(candidate, [1000, 50000])` equals `estimateEvalCost(2, {inputChars: avg(instr+400+min(chars,12000))})` (:261-274).
* `costPhrase`: null→null, 0.004→`"under a cent"`, 0.4→`"about $0.40"`, 1.234→`"about $1.23"` (:278-283).
* `runDeepEval({candidate, posting, overrides:{model: mock}})`: the mock returns `JSON.stringify(evaluation)`; result score 84, recommendation "apply", invented citations dropped; the prompt sent contains the title and `[work-1]` (:287-313).
  **Hidden env dependency:** `callLLM` always calls `modelRef(purpose)` and `providerOf(purpose)` (llm/call.ts:45-48) even when a model override is passed, so `LLM_EVALUATE` or `LLM_DEFAULT` must resolve to a provider whose key is set, else `ModelNotConfiguredError` (models.ts:90-111). The mock is not ledger-wrapped, so no DB write. Also acquires a rate-limiter slot.

### 2.7 discovery.test.ts — company discovery pure helpers
`findAtsLinks(html)` discovery.ts:52 → `[{source, slug}]`; `hasUnsupportedAts` :71; `normalizeDomain` :76; `findCareersLinks(html, baseUrl)` :102; `slugCandidates(name)` verify.ts:71.
* Greenhouse hosts `job-boards.greenhouse.io/<slug>`, `boards.greenhouse.io/<slug>/jobs/..`, embed `boards.greenhouse.io/embed/job_board?for=<slug>`; Lever `jobs.lever.co/<slug>`; Ashby `jobs.ashbyhq.com/<slug>/...` (:12-39).
* De-duplicates (20 links → 1) (:41-49); multiple distinct boards (:51-56); **repeated calls give the same result despite global regexes** (lastIndex reset) (:58-64) — irrelevant in Python but keep the test; none → `[]` (:66-70).
* `hasUnsupportedAts`: myworkdayjobs.com and smartrecruiters.com → true; lever → false (:73-86).
* `findCareersLinks`: absolutises `/about/careers`; recognises `/jobs`, `/join-us`, `/work-with-us`; ignores `javascript:`/`mailto:`; ignores `/pricing` (:88-117).
* `normalizeDomain`: strips scheme/www/path, lowercases → `stripe.com`; keeps meaningful subdomain `jobs.acme.co.uk`; blank → null (:119-140).
* `slugCandidates`: `"Match Group"` → first `matchgroup`, includes `match-group`, `match`; `"Stripe, Inc."` → `stripe`; `"Ben & Jerry's"` → `benandjerrys`; blank → `[]` (:142-158). Implementation (verify.ts:71-97): lowercase, strip `'’`, `&`→`and`, remove legal suffix words `inc|llc|ltd|corp|corporation|co|gmbh|plc`, split on non-alnum; candidates `[joined, hyphenated, words[0], joined+"inc", joined+"hq", joined+"jobs", joined+"careers"]` deduped, length ≥ 2.

### 2.8 follow-up-db.test.ts — when a chat follow-up is queued or held (DB)
Helper `chatRun()` = `createConversation(userId, "Find roles")` + `createRun({userId, kind:"search", status:"completed", input:{prompt:"roles at Anthropic"}, output:{candidates:3}, conversationId})` (:37-48).
* `emit({userId, runId, kind:"run.completed", message})` on a chat-started run → `send("chat.follow_up", {userId, runId, eventId}, {singletonKey: "follow-up-<eventId>"})` exactly (:55-61). **Contract: singleton-key de-dup on the queue.**
* No follow-up for a run without `conversationId`, nor for non-final kinds like `scoring.progress` (:63-71).
* `replyInFlight(conversationId)`: false with no messages; true after a saved `user` message; false after an `assistant` message (:75-88) (via `saveMessages(userId, convId, [{id, role, parts:[{type:"text", text}]}])`).
* `runChatFollowUp({userId, runId, eventId})` → `"skipped"` when the run is cancelled (:90-96) or the userId does not own it (:98-103). Never reaches a model.

### 2.9 follow-up.test.ts — the automatic update prompt text
`describeUpdate(run, event, approval|null, matches?)` follow-up.ts:276 → `{label, text}`.
* `approval.requested` + `deep_eval_batch` → label `"Search paused for your go-ahead"`; text contains `"[Automatic update about run run-1, not written by the user]"`, `"Entry-level roles at Anthropic"` (from brief via describeBrief), the role line `"- Software Engineer, Research Data Platform at Anthropic (78), San Francisco, CA: Strong data platform overlap"`, `"do not call list_jobs"`, `"Do not answer the approval yourself"`, `"can only look things up"` (:22-46).
* `shortlist_proposal` → label `"Deep review done"`, lists payload item titles `"- Software Engineer at Anthropic\n- Fellow at Anthropic"` when no match details (:48-62).
* `run.completed` → `"Search finished"`, contains `"limit 5"`; zero candidates → contains `"widen the search"` and **not** `"list_jobs"` (:64-76).
* `tailor.failed` → `"Tailoring stopped"`, `"Tailoring your resume for Engineer at Stripe stopped with an error: The model timed out."`, `"offer to try again"` (:78-87).
* `tailor.done` → `"Tailored resume ready"`, `"Tailoring your resume for Engineer at Stripe is done."` (:89-97).

### 2.10 format.test.ts — presentation helpers (FRONTEND)
`lib/format.ts`. Stays a frontend Vitest test (see §9). Assertions:
* `formatSalary`: `{148000,216000,USD,year}` → `"$148k–216k"`; numeric strings `"190000","190000"` → `"$190k"`; hourly `55.5–70` → `"$55.50–70 an hour"`; `CAD 90000` → `"CAD 90k"`; nulls → `null` (:15-39).
* `formatLocation`: `(["San Francisco, CA"],"hybrid")` → `"San Francisco, CA, hybrid"`; `(["Remote, USA"],"remote")` → `"Remote, USA"`; 3 locations → `"Seattle, WA + 2 more"`; `([], "remote")` → `"Remote"`; `(null,null)` → `"Location not stated"` (:41-60).
* `timeAgo(value, now)`: `<45s` "just now", `5m ago`, `3h ago`, `2d ago`, ≥14 days → `"Aug 12"` (en-US short; year added only if different year); null → `""` (:62-79).
* `fitLevel([null,20,55,70,80,90])` → `[0,1,2,3,4,5]` (thresholds 90/80/70/55) (:81-85).
* `formatUsd(0)` `"$0"`, `0.004` `"<$0.01"`, `0.756` `"$0.76"`; `formatDuration(42000)` `"42s"`, `125000` `"2m 5s"`; `plural(1,"role")` `"1 role"`, `plural(1200,"role")` `"1,200 roles"` (toLocaleString), `plural(2,"match","matches")`; `initials("Kestrel Logistics Group")` `"KL"`, `"airbyte"` `"AI"`, `""` `"?"`; `nameHue` stable (:87-107).

### 2.11 interview.test.ts — preferences schema and checklist
`PreferencesSchema` interview.ts:46 (every field `nullable().default(null)` via `stored()` :43-44), `EMPTY_PREFERENCES` :60, `SLOTS` :99 (8 slots; required: target_roles, seniority, locations, remote_pref, work_auth; optional: salary_floor, start_date, constraints[must_haves+dealbreakers]), `InterviewStateSchema` :165 (`version` default INTERVIEW_VERSION, `skipped` default []), `interviewProgress` :199.
* Old row `{target_roles, seniority}` parses; missing keys default to `null` (**tolerance for absent keys — a failed parse would fall back to empty preferences and discard answers**) (:13-24); `{}` → EMPTY_PREFERENCES (:26-28); wrong types still rejected (`seniority:"emperor"`, `target_roles:"engineer"`) (:30-38).
* Fresh user: answered 0, total = SLOTS.length (8), not complete, blocking non-empty (:42-51).
* Declined (`skipped:["salary_floor"]`) counts as answered and leaves outstanding (:53-60); declining a required slot (`work_auth`) removes it from outstanding but **keeps it blocking** (:62-70); empty array = unanswered (:72-78); constraints slot filled when **either** field set (`isFilled` uses `some`, interview.ts:181-188) (:80-86); complete once every slot answered or declined (:88-105).
* `InterviewStateSchema.parse({})` → `EMPTY_INTERVIEW_STATE` (:108-111).

### 2.12 jsearch.test.ts — aggregator normalisation
Fixture `jsearch/entry-level-sf.json` shape `{data: {jobs: JSearchJob[], cursor: string}}`. Functions jsearch.ts: `atsFromApplyOptions` :112, `employerSlug` :139, `normalizeJSearchJob` :149.
* Envelope has a non-empty string `cursor` (v5 cursor paging, not page numbers) (:18-25).
* ids `jsearch:<employerSlug>:<job_id>` (3 parts), `source "jsearch"` (:28-34); `descriptionRaw === job_description` (:36-38); text ≤ raw length (:40-47); `job_is_remote === true` → `remote "remote"` (:49-53).
* Implausible salary (`90–120`, period YEAR) → `salary.min null`; plausible `150k–200k` → `{min,max,period:"year"}` (:55-83).
* Aggregator copy (`"Software Engineer, Payments (Remote)"`, `"Stripe, Inc."`, city/state/country fields) has the same `dedupeKey` as board-like copy (`job_location: "San Francisco, CA"`) (:85-104).
* `atsFromApplyOptions`: finds greenhouse slug among `apply_options[].apply_link`, or lever/ashby from `job_apply_link`; only aggregators (indeed/linkedin) → `null` (:107-151).
* `employerSlug("Stripe, Inc.")` `"stripe-inc"`, `"Ben & Jerry's"` `"ben-jerry-s"`, blank/`"!!!"` → `"unknown"` (never empty) (:153-163).

### 2.13 normalize.test.ts — location, remote, salary parsing
normalize.ts: `detectRemote` :75, `parseLocation` :104, `primaryLocation` :163, `parseSalary` :205, `isPlausibleSalary` :273.
* `parseLocation`: `"San Francisco, CA"` → `{city, region:"CA", country:"US", metro:"bay-area"}`; `"Austin, Texas"` → region `TX`, metro null; `"SF Bay Area"` → city null, metro bay-area; suburbs (Mountain View, Oakland, Palo Alto) → bay-area; strips `(Remote)` / `- Hybrid` from city; `raw` keeps original string incl. whitespace; `"Remote"` → metro null, country null; `"London, United Kingdom"` → country `GB`, metro null (:11-71).
* `primaryLocation`: prefers a recognised location (San Jose over Dublin); else first; `[]` → null (:73-87).
* `detectRemote({isRemoteFlag?, location?, title?, description?})`: explicit flag wins; "Hybrid Remote" → hybrid; remote from location or title; bare city → onsite; description mention does not outvote location; description used only when nothing else; `{}` → unknown (:89-136).
* `parseSalary(text)`: `$150,000 - $200,000 per year` → `{150000,200000,USD,year}`; `$180-220k` → 180000/220000 (k carries to both ends); `$55 - $70 per hour` → hour; `£` → GBP, `EUR` suffix → EUR; year ranges (`2015 - 2020`), headcounts (`5 - 10 engineers`), backwards ranges → null; no range → all null; null input → min null (:138-191).
* `isPlausibleSalary`: annual `90/90` false (figure in thousands; Ashby/Vanta real case); `180k–220k` true; hourly 35–38 true but same numbers annual false; backwards or empty false (:193-237).

### 2.14 prefilter.test.ts — title filters and Bay Area regex
prefilter.ts: `escapeForRegex` :278, `guardSeniority(criteria, seniority, roles, opts?)` :230, `withoutLevel` :265, `titleMatches(title, criteria)` :577, `BAY_AREA_PLACES` :460 (Postgres regex string with `\y`). `DerivedCriteria = {title_include, title_exclude, keywords}`.
* `escapeForRegex("C++ engineer")` compiles; `"sr. engineer"` does not match `"srx engineer"` (:21-30).
* `titleMatches`: any include term (case-insensitive); exclusions win; no include terms → keep everything ("better to score a wide set than silently return nothing") (:32-86).
* `guardSeniority(proposed, "entry", ["software engineer"])` excludes senior titles however written (`Staff+`, `Staff + Sr.`, `Sr.`, `Team Lead`, `Engineering Manager`), keeps plain/early-career titles incl. `"Senior Software Engineer, New Grad Program"`, `"Anthropic Fellows Program, ML Systems"`, word-boundary safety (`SRE` ≠ `sr`, `Leadership` ≠ `lead`) (:88-116); does not exclude a senior word the requested role contains (`associate product manager`) (:118-127); with `{companies:true}` includes every early-career title (fellows, rotational) but `Internal`/`International` are not `intern` (:129-140); other seniorities/null → unchanged (:142-145).
* `withoutLevel`: drops leading level words (`senior`, `Sr. Staff`, `entry-level`) but not `lead generation`, not trailing qualifiers (:148-156).
* `BAY_AREA_PLACES` (converted `\y`→`\b`) matches `"Seattle, San Francisco, New York"`, `"SEA, SF, NY, Toronto"`, `"San Francisco HQ"`, `"US-Remote, Chicago, Seattle, San Francisco"`, `"Palo Alto, CA"`; not NYC, Seattle WA, Dublin, `"Sfax, Tunisia"` (:158-176). **In Django these strings are still used inside SQL `~*`; keep them Postgres-regex and convert in tests.**

### 2.15 pricing.test.ts — price table
pricing.ts: `rateFor(ref, at=now)` :109, `costUsd(ref, tokens, at)` :135, `isPriced` :159, `hourInBand` wraps midnight (:104-106).
* Unknown model → `rateFor` null, `isPriced` false (:13-16).
* DeepSeek flash at 16:00Z input 0.3 (peak), 20:00Z 0.15 (off-peak), 02:00Z 0.3 (band 17:00–00:00 does not include 02:00 → fallback peak) (:18-26); flat-rate model independent of hour (:28-32).
* `costUsd("openai:gpt-5.6-luna", 1M in, 1M out)` ≈ 1.4; cached input at cached rate (1M cached → 0.02 vs 0.2); Anthropic haiku-4-5 cache writes 1M → 1.25; reasoning tokens not double-charged (terra 1M out + 500k reasoning → 12); unpriced → 0 (no throw); DeepSeek off-peak exactly half (:35-93).

### 2.16 resume-edits.test.ts — provenance stamping
`stampUserEdits(next, previous|null)` edits.ts:67.
* Untouched item keeps `source` (:21-25); changed field → `source "user"` (:27-34); new id → `"user"`, old kept (:36-46); changed bullet list counts (:48-54); reordering items keeps provenance (`linkedin_pdf` preserved) (:56-67); **key order ignored** (jsonb does not preserve key order) (:69-81); missing optional vs explicit `undefined` equal (:83-89); `previous === null` → provenance unchanged (:91-96). Python: compare canonicalised dicts (drop None/absent equivalently).

### 2.17 resume-template.test.ts — the HTML resume (used for preview + PDF)
template.ts: `escapeHtml` :46, `safeHref` :63, `displayUrl` :78, `dateRange` :88, `renderResumeHtml(resume, {density?, title?})` :276.
* `escapeHtml` escapes `& < > " '` → `&amp; &lt; &gt; &quot; &#39;`; null/undefined → `""` (:64-75).
* `safeHref`: bare domain → `https://`, keeps http(s), email → `mailto:`, keeps `mailto:`; rejects `javascript:`, `data:`, `"not a url"`, `""` → null (:77-91).
* `displayUrl("https://www.linkedin.com/in/dana/")` → `"linkedin.com/in/dana"`; `dateRange` joins with `" – "` (en dash), single side alone, none → `""`, stored text verbatim (:93-104).
* Document: starts `<!doctype html>`, has `<style>`, `@page { size: Letter; margin: 0.5in 0.6in; }`, no `<script|<link |<img `, `<title>Dana Reyes — Resume</title>` (:109-115); section order name, Summary, Experience, Projects, Education, Skills (:117-123); contact links `<a href="mailto:dana@example.com">dana@example.com</a>`, `<a href="https://github.com/dana">github.com/dana</a>`, LinkedIn display, `href="tel:5550134` (:125-130); verbatim text incl. `"B.S. in Computer Science"` (:132-137); skills grouped by first-appearance category `Languages:</span> TypeScript, Python` before `Data:` (:139-142); uncategorised → `<p class="skills-line">Go, Rust</p>` (:144-154); hostile text escaped everywhere, `javascript:` link not linked (:156-183); empty sections omitted (:185-193); `density:"compact"` adds `<main class="page compact">` and otherwise identical text (:195-200); custom title (:202-204).
  For Django: either port to a Jinja2/Django template with autoescape or a Python string builder; **exact HTML is pinned by these tests**, port them byte-for-byte where the strings matter.

### 2.18 search-gates.test.ts — search continuation pipeline (DB, heaviest)
Mocks (:17-77): `vi.hoisted` state `{failing:Set, evaluated:[]}`; queue `send`; **partial module mock of `@/lib/ranking/deep-eval`** keeping real exports but replacing `loadEvalCandidate` (returns a fixed candidate, factIds `{"work-1"}`, preferences with seniority mid etc.) and `evaluateMatch({userId, matchId})` (reads `getMatchRows`, throws `"That match does not exist."` or `"model timed out"` for ids in `failing`, builds a DeepEval with `score = fastScore ?? 50`, recommendation by thresholds ≥85 strong, ≥70 apply, ≥50 maybe else skip, summary `Why <title> fits.`, persists via `saveDeepEval`, records id). In pytest use `monkeypatch`/`unittest.mock.patch` on the Python module attribute the pipeline calls.
Data: borrows up to 60 real `jobs` rows with `is_active = true` (:130-140), consumed cumulatively by `takeJobs` (35 total across file); throws `"Not enough active jobs in the dev database"` (:123-128). `beforeEach` deletes the user's `job_matches` (:146-153). `seed([{stage?, fastScore?, deepEval?}])` inserts `job_matches` rows `{userId, jobId, stage (default "found"), fastScore, deepScore: deepEval.score, deepEval}` (:162-181). `searchRun(mode, jobIds, stats)` = `createRun({kind:"search", status:"running", mode, input:{candidateJobIds}, stats:{phase:"scoring", ...}})` (:183-196). `settings()` = `{...DEFAULT_SETTINGS, scoreThreshold:55, deepEvalTopN:3}` (:235-240).
Assertions:
* `selectForDeepEval(userId, {jobIds, minFastScore, limit})` (matches.ts:359): only this run's job ids, open stages (found/shortlisted; not archived), not yet deep-evaluated, `fastScore ≥ threshold`, unscored excluded, ordered by fastScore desc; limit 1 → top; limit 0 or empty jobIds → `[]`; rows carry `descriptionChars ≥ 0` (:243-272).
* `isShortlistCandidate({stage, recommendation})` (matches.ts:425): true only for `found` + `strong|apply` (:276-284). `listShortlistCandidates(userId, matchIds)` returns same rule from DB, best deep score first, `headline` = evaluation summary (:286-297). `moveMatchesFromStage(userId, ids, "found", "shortlisted")` only moves rows still in `found` (conditional), returns moved ids (:299-306).
* approve_each full flow (:313-434): `gateDeepEvaluation(run, settings, jobIds)` (search-run.ts:361) creates `deep_eval_batch` approval, `pending`, title matches `/^Deep-evaluate 3 roles \((about \$\d+\.\d{2}|under a cent)\)$/`; payload items ordered by fastScore `[m0(92), m4(88, shortlisted), m1(78)]`, item `{jobId, fastScore}`; `estimate.model === "openai:gpt-5.6-terra"` (**env-coupled**), `usd > 0`, `usd ≈ perItemUsd*3`, `note null`; run `awaiting_approval`, `stats ⊇ {phase:"awaiting_evaluation", toEvaluate:3}`; nothing sent; `approval.requested` event message starts `"Waiting for your go-ahead to deep-evaluate 3 roles"`. Decide subset `[m0,m1]` via ui → `send("search.evaluate", {userId, runId, approvalId})`; `continueSearchEvaluate(payload)` (evaluate-run.ts:231) evaluates exactly m0,m1; creates `shortlist_proposal` approval `"Shortlist 2 roles"` with items `[matchId, recommendation, deepScore]` = `[[m0,"strong",92],[m1,"apply",78]]`, summary `^Why `; run `awaiting_approval`, stats `{phase:"awaiting_shortlist", evaluated:2, evaluationFailed:0, recommended:2, proposedShortlist:2}`. **Re-delivery of the same continuation is a no-op** (still 2 approvals). Decide `[m0]` via chat → `continueSearchShortlist(lastSent("search.shortlist"))` (evaluate-run.ts:432) → stages `[shortlisted, found, shortlisted(pre-existing)]`; run `completed`, `finishedAt` set, stats `{phase:"done", shortlisted:1}`, output exactly `{candidates:5, newMatches:5, scored:5, autoArchived:1, evaluated:2, shortlisted:1, topMatchIds:[m0,m4,m1,m2]}` (deep score else fast, closed excluded); event kinds exactly `[approval.requested, approval.decided, evaluation.start, evaluation.progress, evaluation.done, approval.requested, approval.decided, shortlist.done, run.completed]`; final message `"Found 5 roles, scored 5, deep-evaluated 2. 2 look worth applying to; 1 shortlisted."`.
* Declined gate → nothing evaluated, run completed, stats `{phase:"done", evaluated:0}`, output `{evaluated:0, shortlisted:0}`, kinds `[approval.requested, approval.decided, evaluation.skipped, run.completed]` (:436-458).
* Cancelled while waiting → continuation does nothing; run `cancelled`; approval `expired` (:460-473).
* Partial failure: stats `{evaluated:1, evaluationFailed:1}`, `evaluation.failed` event, shortlist proposal only the success; all fail → run `failed`, `error === "Deep evaluation failed for every role: model timed out"`, last event `run.failed` (:475-506).
* Autopilot: gate 1 `auto_approved`/`autopilot`, run stays `running` with `{phase:"evaluating", toEvaluate:3}`; gate 2 auto-approved; stages `[shortlisted, shortlisted, found]` (strong, apply, maybe); output `{evaluated:3, shortlisted:2}`; no `approval.requested`; two `approval.auto` (:510-543).
* Nothing eligible → no approvals, no sends, completed with `{evaluated:0, topMatchIds: array}` and message `"Found 2 roles, scored 2. 1 was archived as too weak."` (:545-564). `deepEvalTopN: 0` → skip entirely, completed (:566-574).
* On demand (:578-622): `startMatchEvaluation(userId, matchIds)` (evaluate-run.ts:515) rejects `NoResumeError` with no resume; after inserting a `resumes` row `{kind:"base", version:1, data:{basics:{name, links:[]}}}` returns `{reused:false, run}` with `run.kind "rank_deep"`, `input {matchIds}`; second call while queued → `{reused:true}` same run, one send; payload `{userId, runId, matchIds}`; `executeMatchEvaluate(payload)` (:566) → run completed, output `{evaluated:1, matchIds}`, stats `{phase:"done", evaluated:1, evaluationFailed:0}`; match deepScore 92 / recommendation strong; final message `` `Evaluated ${title} at ${company}: 92, a strong match.` ``; repeat delivery does not re-evaluate. Foreign/junk ids → throws `"There is no such match to evaluate."` (:624-628).
* Wording: `summarizeSearchRun(stats)` (evaluate-run.ts:155) seven exact strings (:632-667), e.g. `"Found 60 roles (3 new), scored 3, deep-evaluated 12. 1 looks worth applying to."`, `"Found 60 roles, scored 60. 8 look strong, 12 are worth a look, 20 were archived as too weak."`, `"No open roles matched your criteria this time."`, `"Found 12 roles (none new). 1 looks strong."`. `describeEvaluations(items, failed?)` (:748) → `"Evaluated Backend Engineer at Stripe: 82, worth applying."` and multi-item `"Evaluated 4 roles: B at Stripe (90, a strong match); D at Stripe (75, worth applying); C at Stripe (60, a maybe); and 1 more. 1 could not be evaluated."` (sorted by score desc, top 3 then "and N more") (:669-695).
* `topMatchIdsForJobs(userId, jobIds, n)` (matches.ts:479): open matches ranked by deep score else fast, `rejected` and unscored excluded (:698-713).

### 2.19 tailor.test.ts — mapping untrusted tailor output
tailor.ts: `TailorOutputSchema` :108 (all fields required, nullable — OpenAI strict mode), `evaluationNotes` :157, `buildTailorPrompt({base, job, notes?, preferences?})` :245, `mapTailoredOutput(base, output)` :331 → `{resume, dropped, changes}`.
* Unknown item id dropped and reported `{section:"work", item_id:"w-invented", text:"Staff Engineer — Stripe"}` (:114-138).
* Work keeps **base order**, projects take **tailor's order** (:140-144); provenance (`source`) copied from base item, blank bullets dropped (:146-151); `basics` copied verbatim (:153-157).
* Skills mapped by case-insensitive name to base skill with base id/category; unknown (`Kubernetes`) dropped & reported; duplicates (`TYPESCRIPT`) ignored (:159-169); repeated item id → first wins (:171-176); impossible date (60 chars) replaced by base value (:178-183); change log passed through trimmed (:185-197).
* `evaluationNotes({deepEval, fastEval})` → `{matched:["Postgres at scale — shown by: 40M-row table"], talkingPoints:[...], gaps:["Kubernetes (not on resume)","Security clearance"]}` (gaps + fast-eval `hard_requirement_misses`); null or malformed → empty lists (:200-227).
* Prompt: instructions contain `"[w1] Engineer II — Kestrel"`, `"<exact_fields>"`, `'"Minor in Statistics."'`; prompt contains `"Title: Backend Engineer"` and `"never claim or imply them:\n- Kubernetes"` (:239-252); prompt length < 11,000 even with a 34k-char description (cap) (:254-257).

### 2.20 token-accounting.test.ts — provider usage shape
`tokensFromProviderUsage(usage)` ledger.ts:35 reads `{inputTokens:{total,noCache,cacheRead,cacheWrite}, outputTokens:{total,text,reasoning}}` (AI SDK provider-level shape).
* noCache/cacheRead split (400/600; out 120) (:26-37); only total → `inputTokens = max(total - cacheRead, 0)` (750) (:39-46); never negative (:48-54); cacheWrite carried to cost (haiku: 1M writes $1.25 + 1000 cached reads $0.0001) (:56-69); reasoning visible but not double-charged (terra 900 out → 900/1e6*12) (:71-84); `undefined` → zeros, cost 0 (:86-90).
  **Port note:** the Python LLM client's usage object will differ (e.g. OpenAI `prompt_tokens_details.cached_tokens`, Anthropic `cache_creation_input_tokens`/`cache_read_input_tokens`); rewrite the normaliser and these tests against the new shapes, preserving: uncached = total − cached when not given, clamp ≥ 0, reasoning counted inside output.

### 2.21 verify.test.ts — the fabrication guard (largest)
verify.ts: `splitSentences` :117, `collectClaims` :124, `extractLinks` :226, `extractMetrics` :314 (→ `[{keys:[...]}]`), `extractTerms` :369, `checkRules(base, tailored)` :519 → `Finding[] {section, item_id, text, reason, source:"rule"|"model", target}`, `type Judge` :698, `verifyTailored(base, tailored, changes, context, {judge?})` :805, `reviseTailored` :927 → `{resume, revisions[{action: "dropped"|"replaced"|"reverted", ...}]}`, `finalizeVerification` :1102, `reconcileChanges` :1134.
Base resume fixture in code with ids `w-kestrel`, `w-northgate`, `w-intern`, `e1`, `p1`, `s1..s5` (:27-106).
* Faithful tailoring passes: reorder/select/subset skills (and all claims verbatim) (:116-127); rephrase keeping every number/name from the same role (`~1.2k` ≈ `1,200`, `Postgres` ≈ `PostgreSQL`, `TypeScript/Node.js`) (:129-138); written vs numeric forms (`two`↔`2`, `40-million`↔`40M`, `tripled`↔`3x`, `3 hours 10 minutes`↔`3h 10m`) (:140-149); summary may draw on anything in resume incl. skills (:151-158).
* Fabrication caught: injected skill → 1 finding `{section:"skills", text:"Kubernetes", source:"rule"}` (:162-169); recased skill not an invention (:171-175); injected employer → target `{kind:"item", section:"work", itemId}` (:177-193); changed date/title/company → targets `{kind:"field", section:"work", itemId, field}` and reason contains `from "March 2023" to "January 2023"` (:195-208); omitting location/GPA allowed, changing GPA flagged (`field:"score"`) (:210-220); invented `40%` though `40` appears elsewhere → target `{kind:"text", key:"work:w-kestrel:highlight:1"}` (:222-232); computed `77%` caught (:234-239); metric moved between roles → reason contains `"this role"` (:241-249); technology not mentioned in that role (even if a skill) → reason matches `/Kubernetes and Kafka/` (:251-258); capitalised first word not a technology (:260-264); invented link/domain caught, real one allowed (:266-277); summary sentences checked against whole resume → targets `summary:sentence:0/1` (:279-287); changed basics → `{kind:"basics", field:"email"}` (:289-293).
* Extractors: `extractMetrics` keys `pct:40`, `x:3`, `n:2000000` (`$2M`), `n:10000` (`10k`), `n:1200`, `n:40000000`, `n:2019`, `n:12`, `n:840`; words `twenty-five`→`n:25`, `six`→`n:6`, `tripled`→`x:3`, `one of the first` → none; `S3, EC2, Q3` not numbers (:296-322); `extractTerms` → `["TypeScript","Node.js","Kafka","C++"]` and `["S3","EC2","Q3"]`, ordinary capitalised first words excluded (:324-332); `extractLinks` → lowercased, `www.`/trailing slash stripped, emails, sorted: `["a@b.co","example.com/x"]`; `Node.js`/`Next.js` are not links (:334-340); `splitSentences` respects abbreviations `B.S.`, `U.S.` (:342-348).
* `reviseTailored`: reverts fields, drops invented items/skills, restores original bullet → actions sorted `["dropped","dropped","replaced","reverted"]`, and the result passes `checkRules` (:352-376); flagged bullet resembling nothing → dropped (:378-386); never duplicates an original already present (:388-399); drops only unsupported summary sentence; if none survive falls back to base summary (:401-413).
* With a judge: judge called once with only rewritten lines that passed rules; `modelChecked 1`; findings sources `["rule","model"]`; `finalizeVerification` → `{passed:true, revised:true, unsupported:[], modelError:null}`, 2 revisions (:420-448); nothing rewritten → judge not called (:450-457); a line the judge skipped (empty Map) → model finding (:459-467); judge throws → `modelError "provider unavailable"`, every rewrite reverted, verification still `passed` (:469-488).
* `reconcileChanges({base, final, changes, revisions})`: removes entries for reverted lines and for invented item ids, filters `evidence_fact_ids` to real ids, and logs rewrites the tailor forgot (Northgate) (:491-551).

---

## 3. Fixtures (`src/test/fixtures/`)

| Path | Size | Used by | Notes |
|---|---|---|---|
| greenhouse/anthropic.json | 50 KB | adapters | real API response (`?content=true`), trimmed; `{jobs:[...]}`; contains the `;`+`|` mixed location |
| greenhouse/stripe.json | 33 KB | **unused** | spare capture |
| greenhouse/_not-found.txt | 0.6 KB | doc only | 404 JSON `{"status":404,"error":"Job not found"}`; empty board = 200 `{"jobs":[],"meta":{"total":0}}`; detect by status |
| lever/matchgroup.json | 70 KB | adapters | bare array; has `lists`, `salaryRange`, `workplaceType` |
| lever/spotify.json | 40 KB | **unused** | |
| lever/_not-found.txt | 0.7 KB | doc only | 404 `{"ok":false,"error":"Document not found"}`; empty account = 200 `[]` |
| ashby/linear.json | 56 KB | adapters | no compensation (`shouldDisplayCompensationOnJobPostings:false`), has secondaryLocations |
| ashby/vanta.json | 64 KB | adapters | has `compensation.summaryComponents` |
| ashby/_not-found.txt | 0.8 KB | doc only | 404 **plain text** "Not Found" (do not JSON-parse before checking status); empty board 200 `{"jobs":[],"apiVersion":"1"}` (apiVersion string vs number 1 on populated boards) |
| jsearch/entry-level-sf.json | 27 KB | jsearch | `{data:{jobs, cursor}}` (v5 cursor) |
| careers-pages/retool.html | 400 KB | careers-page | server-rendered careers page with posting links |
| careers-pages/crusoe.html | 273 KB | careers-page | JS-rendered page, only nav links |
| resumes/single-column.{html,pdf} | 6 KB / 71 KB | **no automated test** | used to verify extraction manually in Step 8 (commit 2d7f29e) |
| resumes/two-column.{html,pdf} | 10 KB / 112 KB | **no automated test** | genuinely interleaving two-column layout |
| resumes/serif-dense.{html,pdf} | 8 KB / 242 KB | **no automated test** | serif/LaTeX-style, letter-spacing, soft hyphens |
| resumes/plain.docx | 11 KB | **no automated test** | real OOXML for the mammoth path |
| resumes/plain.docx.gen.mjs | 232 lines | regenerator | `pnpm exec node src/test/fixtures/resumes/plain.docx.gen.mjs`; uses devDependency `docx` (Packer.toBuffer), Calibri, writes `plain.docx` next to itself |

The `_not-found.txt` notes are the authoritative description of each ATS's 404 vs empty-board
behaviour (the request line also records the UA `Foray/0.1 (job search agent; local development)`).
**Recommendation:** the resume fixtures are the right place to add Python extraction tests, because the
Python PDF/DOCX libraries (pypdf/pdfplumber/PyMuPDF, python-docx/mammoth-py) will produce different text
than `unpdf`/`mammoth`, and the garble heuristic (`resume/parse/garble.ts`) thresholds must be re-validated.

---

## 4. Scripts (`scripts/**`) — every one becomes a Django management command

All scripts: `import "@/lib/load-dotenv"` first (or `../src/lib/load-dotenv`), run with `tsx`, and end the pg
pool. Errors: most print `error.message` and `process.exit(1)`.

### 4.1 `scripts/dev-session.ts` (57 lines) — mint a browser session without a password
Usage: `pnpm tsx scripts/dev-session.ts [email]` (dev-session.ts:15).
* Refuses when `env.NODE_ENV === "production"` → error `"dev-session is for local development only."` (:23-25); requires `BETTER_AUTH_SECRET` (:26).
* Target user: by `email` if given, else **first row of `user`** (no ORDER BY) (:28-32); errors `No user with email X.` / `No users yet.`.
* `token = randomBytes(24).toString("base64url")` (32 chars) (:34); inserts `session {id: randomBytes(16).hex, token, userId, expiresAt: now + 7 days, userAgent: "dev-session script"}` (:35-41) (`created_at/updated_at` default now).
* Cookie value = `encodeURIComponent(token + "." + base64(HMAC-SHA256(secret, token)))` (standard base64 with padding, then URL-encoded) (:43-45).
* Prints `user: <email>` and `cookie: better-auth.session_token=<value>` (:47-48). CLAUDE.md: drop the `cookie: ` prefix and set via `document.cookie` in the browser pane.
* Side effects: one `session` row. Exit 1 on error.
* Django port: `manage.py dev_session [email]` — must mint whatever session the new auth layer accepts (Django `SessionStore` + `sessionid` cookie, or keep Better-Auth-compatible signed token if Django validates Better Auth sessions). Keep: refuse in production, default to first user, print a single `cookie: name=value` line so the browser-pane workflow keeps working.

### 4.2 `scripts/set-password.ts` (91 lines) — forgotten-password reset at a hidden prompt
Usage: `pnpm auth:set-password you@example.com` (set-password.ts:12).
* Requires email arg (`Usage: pnpm auth:set-password <email>`) (:22-23); user must exist (:25-26).
* Uses Better Auth's own context: `ctx.internalAdapter.findAccounts(userId)`; must have an account with `providerId === "credential"` else `"<email> does not sign in with a password."` (:28-32).
* Length bounds from `ctx.password.config` = Better Auth defaults **min 8, max 128** (node_modules/better-auth/dist/context/create-context.mjs:186-187) (:34, 37-41).
* Two hidden prompts (`New password for <email>: `, `Type it again: `); mismatch → `"Those did not match. Nothing was changed."` (:36, 42-44).
* `ctx.internalAdapter.updatePassword(userId, await ctx.password.hash(pw))` (:46) → updates `account.password` of the credential account. **Hash format** (@better-auth/utils password.node.mjs): `"<saltHex>:<keyHex>"`, salt = 16 random bytes hex-encoded and **the hex string itself is the scrypt salt**, password NFKC-normalised, scrypt N=16384, r=16, p=1, dkLen=64. Existing sessions kept.
* Prints `Password updated. Sign in at <baseURL without /api/auth>/sign-in` (:47).
* `promptHidden` (:51-83): requires a TTY (`"Run this in a terminal: the password is typed at a hidden prompt."`); raw mode; Enter ends; Ctrl-C (`\u0003`) → `"Cancelled."`; backspace (`\u007f`/`\b`) deletes.
* Django port: `manage.py set_password <email>` with `getpass`; if Django adopts Better Auth's existing hashes, implement a custom `PasswordHasher` for the scrypt format above (or rehash on next login). Django's built-in `changepassword` is the natural replacement if Django owns auth.

### 4.3 `scripts/seed-companies.ts` (93 lines) — load the company registry
Usage: `pnpm seed:companies` / `pnpm seed:companies --fetch` (seed-companies.ts:16-17).
* Reads `src/lib/companies/seed.json` relative to cwd (:30-32): **225 entries** (greenhouse 125, ashby 90, lever 10), each `{name, slug, sourceType: "greenhouse"|"lever"|"ashby", atsSlug, domain, tags[]}` (all have domain and tags).
* For each: `upsertCompany({name, slug, domain ?? null, sourceType, atsSlug, careersUrls: [], tags ?? [], status: "active", discoveredVia: "seed"})` sequentially (:37-50). `upsertCompany` (queries/companies.ts:61-91): find existing by `(sourceType, atsSlug)` else by `slug`; update name/domain/careersUrls/status/needsBrowser and **merge tags (never replace)**, `updatedAt = now`; else insert. Idempotent.
* Prints `seeding N companies…`, `registered N`, then per-status counts from `countByStatus()` (`select status, count(*)::int group by status`) (:34, 52-55).
* `--fetch`: `listCompanies({status:"active"})` (default **limit 500**, ordered by name) → `refreshCompanies(active)` (refresh.ts:97, pool of 6, per-company `refreshCompany` → fetch board + `ingestJobs`) and prints `"<ok> boards ok · <gone> gone · <failed> errored in <s>s"`, `"<jobs> postings seen → <inserted> inserted, <updated> updated"`, and up to 10 `not-found`/`error` lines (:57-88). Network: Greenhouse/Lever/Ashby public APIs. No LLM for seeded (ATS) companies.
* Note: `void main()` without catch → unhandled rejection on error (:93).
* Django port: `manage.py seed_companies [--fetch]`; seed JSON can stay as a data file (or a data migration).

### 4.4 `scripts/fetch-board.ts` (106 lines) — print what an adapter makes of one board (no DB)
Usage: `pnpm tsx scripts/fetch-board.ts <greenhouse|lever|ashby> <slug> [--json] [--limit n]` (:12-15).
* Arg parsing (:17-31): positional = args not starting with `--` (so a `--limit` value is also "positional", harmless only when it comes last); `--json` flag; `--limit` default 5.
* Bad args → usage on stderr, exit **2** (:37-42). `BoardNotFoundError` → `No <source> board at slug "<slug>".`, exit **1** (:50-55).
* `--json`: `JSON.stringify(result.jobs.slice(0, limit), null, 2)` (NormalizedJob objects, Dates as ISO) (:60-63).
* Text mode: header `"<source>:<slug> — <n> postings in <ms>ms"`, then per job: title, `id`, `where` (locations joined ` | ` + `[remote]` + `metro=`), `pay` (`<currency><min>–<max>/<period>` with toLocaleString, or `—`), `posted` (YYYY-MM-DD + `type=`), `text raw N chars → cleaned M`, `url`, optional `apply`, `dedupe <first 12 hex>…`; totals line `"totals: N postings · X with pay · Y remote · Z Bay Area"` (:65-103).
* Django port: `manage.py fetch_board`.

### 4.5 `scripts/verify-boards.ts` (117 lines) — check that slugs are real (no DB)
Usage (:15-17): `--source <s> --slugs a,b` | `--guess Name...` | `--file list.json` (array of `{name, source?, slug?}`; only `name` used), plus optional `--json`.
* `CONCURRENCY = 6`; own `pooled()` (duplicate of lib/pool.ts) (:20-41).
* Source+slugs: `verifyBoard(source, slug)` (companies/verify.ts:20) → prints `status.padEnd(10) source:slug  [N jobs] [error]` (:56-66). Outcomes: `ok` (jobs>0), `empty`, `not-found` (BoardNotFoundError), `error`.
* Guess/file: for each name, try `slugCandidates(name)` in order; each candidate `findBoard(candidate)` hits **all three providers in parallel** and prefers ok over empty (verify.ts:49-62); first ok wins (:87-93). Prints `✓ <name padded 28> source:slug (N jobs)` or `✗ <name> no board found`, then `"\n<found>/<total> resolved"`; `--json` prints `[{name, sourceType, atsSlug, slug}]` (seed-entry shape minus domain/tags) (:95-114).
* No args → usage, exit 2 (:78-83).
* Django port: `manage.py verify_boards`.

### 4.6 `scripts/llm-smoke.ts` (91 lines) — one call per configured purpose
Usage: `pnpm llm:smoke [purpose ...]` (:15-16).
* Prints providers with credentials (`configuredProviders()`, registry.ts:77) or `"no provider credentials found — set one in .env (see .env.example)"` (:25-30).
* For each `configuredPurposes()` (models.ts) filtered by argv: if unresolved → `✗ <purpose> <error>` and count failure (:39-44); else `generateStructured(purpose, Answer, {instructions:"Answer factual questions. Respond only with the requested fields.", prompt:"Which city is the Golden Gate Bridge in?"})` with schema `Answer = {city: string (describe "The city named in the question."), confident: boolean}` (:19-22, 50-58).
* Success line: `✓ <purpose padded 9> <ref padded 32> <ms>ms  in=<inputTokens> cached=<cacheReadTokens> out=<outputTokens>[  (retried)][  (unpriced — add it to pricing.ts)]  → <json>` (:60-69). Failure: `✗ <purpose> <ref>: <message>`.
* **Writes real `llm_calls` ledger rows** (model is ledger-wrapped) with null user/run. Ends with a hint SQL `select purpose, model, input_tokens, cached_input_tokens, output_tokens, cost_usd from llm_calls order by created_at desc;` (:80-85). Exit 0 if all succeed else 1 (:87-88). Costs real money (tiny).
* Django port: `manage.py llm_smoke [purposes]`.

### 4.7 `scripts/model-bakeoff.ts` (154 lines) — compare models on fixed fixtures
Usage: `pnpm llm:bakeoff --purpose score --models a:b,c:d [--reference a:b] [--reasoning <effort>]` (:21-22, 80-102). Missing purpose/models → usage, exit 2.
* Exports `FixtureRun<T> {run(modelRef, {reasoning?}) → {outputs, tokens, latencyMs, schemaFailures}, agreement(candidate, reference) → 0..1}` and `FIXTURES` registry; only `score` exists (:37-78); placeholders noted for `extract` and `tailor`.
* Warns for unpriced models (:107-113). No fixture for purpose → error, exit 1 (:115-123).
* Reference run first if given (:125-127). Per model: `cost = costUsd(model, tokens)`; row `{model, cost "$x.xxxx", per500 "$(cost/total*500).2f", latency "s.s s", reasoningShare "%", schemaFail "f/total", agreement "0.00" or "—"}`; `console.table(rows)` (:129-150).
* Score fixture (`src/lib/ranking/bakeoff-fixture.ts`): `scoringSample(limit=20)` = `select id,title,company_name,locations,remote,description_text from jobs where description_text is not null and length(description_text) between 1500 and 12000 order by id limit 20` (:48-64) — **needs a populated `jobs` table**; fixed RUBRIC + CANDIDATE prompt strings (:27-38, 107-114, description sliced to 4000 chars); calls `generateStructured("score", FastScore, …, {}, {model: raw registry model, reasoning, providerId})` (:103-118) — overrides bypass ledger wrapping (no `llm_calls` rows) but `callLLM` still requires `LLM_SCORE`/`LLM_DEFAULT` to resolve with a key; failures counted as `schemaFailures`; `scoreAgreement` = `max(0, 1 - mean|Δscore|/100)` over shared job ids, 0 if none (:162-176).
* Django port: `manage.py llm_bakeoff`.

### 4.8 `.cx.ts` (20 lines, tracked scratch file)
Matches the `.gitignore` rule `/.*.ts` ("scratch scripts used while verifying a step", .gitignore:41-42) yet is tracked. Hard-codes a real userId (redacted), creates a `search` run (`mode approve_each`, `status queued`, input `{prompt:"cancel test"}`), sends `QUEUES.search`, cancels after 1.2 s with `requestCancel`, waits 12 s and prints final status/stats. **Do not port**; it documents the expectation that the worker notices a cancel between stages.

### 4.9 Migrations entry point `src/lib/db/migrate.ts` (23 lines)
`drizzle-orm/node-postgres/migrator` `migrate(db, {migrationsFolder:"./drizzle"})`, logs `[migrate] applying migrations from ./drizzle` / `[migrate] up to date`, exit 1 on failure (:11-23). Used by `pnpm db:migrate` and the compose `migrate` service. History: `drizzle/0000_calm_bloodscream.sql` (16 tables: account, session, user, verification, companies, job_matches, jobs, documents, profiles, resumes, agent_events, approvals, runs, conversations, llm_calls, messages), `0001` (drops `account_provider_account_idx`), `0002` (`api_usage`), `0003` (`system_state`, `profiles.settings jsonb default '{}'`); journal `drizzle/meta/_journal.json`. Drizzle records applied migrations in `drizzle.__drizzle_migrations`. Replacement: `manage.py migrate` with an initial migration that is `--fake-initial`-able against the existing schema (or `managed = False` models).

---

## 5. `package.json` scripts (package.json:5-29) and replacements

| Script | Today | After port |
|---|---|---|
| `dev` | `next dev` (:6) | unchanged (frontend only) + rewrites to Django |
| `build` / `start` | `next build` / `next start` (:7-8) | unchanged |
| `lint` | `eslint` (:9) | frontend eslint; add `ruff` for Python |
| `typecheck` | `tsc --noEmit` (:10) | frontend; add `mypy`/`pyright` optional |
| `db:up` / `db:down` | `docker compose up -d db` / `docker compose down` (:11-12) | unchanged |
| `docker:prod` | `docker compose --profile prod up --build` (:13) | unchanged name, new services |
| `db:generate` | `drizzle-kit generate` (:14) | `manage.py makemigrations` |
| `db:migrate` | `tsx src/lib/db/migrate.ts` (:15) | `manage.py migrate` |
| `db:studio` | `drizzle-kit studio` (:16) | Django admin / psql |
| `auth:generate` | `better-auth generate --config src/lib/auth/index.ts` (:17) | n/a (Django auth models) |
| `auth:set-password` | `tsx scripts/set-password.ts` (:18) | `manage.py set_password` |
| `dev:worker` | `tsx watch src/worker/main.ts` (:19) | Python worker with autoreload (e.g. `watchfiles`) |
| `dev:all` | `concurrently -n next,worker -c cyan,magenta "pnpm dev" "pnpm dev:worker"` (:20) | 3 processes: next, django, worker (honcho/Procfile or concurrently) |
| `test` / `test:watch` | `vitest run` / `vitest` (:21-22) | frontend vitest + `pytest` |
| `llm:smoke` / `llm:bakeoff` | tsx scripts (:24-25) | management commands |
| `fetch:board` / `verify:boards` / `seed:companies` | tsx scripts (:26-28) | management commands |

Other package.json facts: `"type":"module"` (:84), `engines.node >=24` (:85-87), `packageManager pnpm@10.34.5` (:83), TypeScript `~5.9`, Next `16.3.5`, `playwright-core 1.63.0` pinned to match the Dockerfile's Playwright image (Dockerfile:7).

---

## 6. Configuration and environment

### 6.1 `src/env.ts` (99 lines) — single source of configuration
`createEnv` from `@t3-oss/env-nextjs` with zod 4. "Both entry points import this … nothing else reads `process.env` directly" (env.ts:4-9) — **one exception**: `LLM_REASONING_<PURPOSE>` is read raw in `llm/models.ts:143`.
Global options: `client: {}`, `experimental__runtimeEnv: {}` (no client vars; "everything intelligent runs server-side", :85-92); **`emptyStringAsUndefined: true`** — `FOO=` means unset (:94-95); **`skipValidation: process.env.SKIP_ENV_VALIDATION === "1"`** for docker build (:97-98). Validation runs at import time; failure throws and the process does not start.

| Var | Validation (env.ts line) | Default | Read by |
|---|---|---|---|
| `NODE_ENV` | enum development/test/production (:27-29) | development | db/index.ts:24,27 (pool max 10 in prod else 5; globalThis cache only outside prod); dev-session.ts:23 |
| `DATABASE_URL` | `z.url()` **required** (:31) | — | db/index.ts:23, queue/boss.ts:17, drizzle.config.ts:12 |
| `BETTER_AUTH_SECRET` | string min 32, optional (:33) | — | auth/index.ts:27, dev-session.ts:26,44 |
| `BETTER_AUTH_URL` | url (:34) | `http://localhost:3000` | auth/index.ts:26 |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | optional strings (:35-36) | — | auth/index.ts:15-23 (Google provider only when both set); sign-in page.tsx:24 (`googleEnabled`) |
| `OPENAI_API_KEY`, `GOOGLE_GENERATIVE_AI_API_KEY`, `DEEPSEEK_API_KEY`, `ANTHROPIC_API_KEY` | optional (:41-44) | — | llm/registry.ts:62-75 (`hasCredential`); SDK providers read them implicitly from process.env |
| `LLM_COMPAT_BASE_URL` | optional url (:45) | — | registry.ts:30-39 (registers `compat` provider only if set); counts as compat credential |
| `LLM_COMPAT_API_KEY` | optional (:46) | — | registry.ts:36 |
| `LLM_DEFAULT`, `LLM_CHAT`, `LLM_EXTRACT`, `LLM_SCORE`, `LLM_EVALUATE`, `LLM_TAILOR`, `LLM_VERIFY`, `LLM_MERGE` | optional, regex `^[a-z0-9-]+:[A-Za-z0-9._/@-]+$` ("provider:model") (:15-21, 52-59) | — | models.ts:46-54, 91 (purpose → own var ?? LLM_DEFAULT; none → `ModelNotConfiguredError`; provider w/o key → error, **never falls back to another vendor**) |
| `LLM_RPM_OPENAI/GOOGLE/DEEPSEEK/ANTHROPIC/COMPAT` | coerce int > 0, optional (:23, 61-65) | limiter `DEFAULT_RPM = 300` (limiter.ts:28) | limiter.ts:64-70 (token bucket per provider) |
| `RAPIDAPI_KEY` | optional (:67) | — | jsearch.ts:245, 286 (`x-rapidapi-key`), 334 |
| `JSEARCH_ENABLED` | enum `"true"|"false"` → boolean (:68-71) — **any other value fails boot** | `"false"` | jsearch.ts:242, 334 |
| `JSEARCH_MONTHLY_BUDGET` | coerce int > 0 (:73) | 180 | jsearch.ts:34 |
| `SEARCH_API_KEY` | optional (:74) | — | companies/discovery.ts:156,165 (Brave `X-Subscription-Token`) |
| `STORAGE_DIR` | string (:76) | `./data` | storage/index.ts:30 (`resolve(env.STORAGE_DIR ?? "./data")` — fallback matters at docker build when validation is skipped) |
| `PDF_BROWSER_CHANNEL` | optional (:81) | — | worker/pdf.ts:43 (force channel; else bundled Chromium, fallback to installed `chrome` on missing executable) |
| `WORKER_CONCURRENCY` | coerce int > 0 (:82) | 4 | worker/main.ts:48 (`heavy = clamp(1, WORKER_CONCURRENCY, 4)`) |
| `LLM_REASONING_{CHAT,EXTRACT,SCORE,EVALUATE,TAILOR,VERIFY,MERGE}` | **not in schema**; raw `process.env`, accepted only if one of `none|minimal|low|medium|high|xhigh`, else silently ignored (models.ts:142-163) | per-purpose defaults chat low, extract low, score minimal, evaluate medium, tailor high, verify low, merge low (models.ts:65-73) | llm/call.ts:54 |
| `SKIP_ENV_VALIDATION` | raw, `"1"` skips (:98) | — | Dockerfile:23 |
| `PORT`, `HOSTNAME`, `NEXT_TELEMETRY_DISABLED` | not in schema; Next standalone server | 3000 / 0.0.0.0 / 1 | Dockerfile:11, 28; compose :50-51 |

Django equivalent: `pydantic-settings` (or django-environ) model with identical names, defaults, the same
`provider:model` regex, empty-string-as-unset, `JSEARCH_ENABLED` strict `"true"|"false"`, and a skip switch
for `collectstatic`/image builds. Keep `LLM_REASONING_*` lenient (ignore invalid).

### 6.2 `.env.example` (107 lines)
Sections: Database (`DATABASE_URL=postgres://foray:foray@localhost:5432/foray`; prod compose overrides host to `db`) (:1-6); Auth (`BETTER_AUTH_SECRET=` "openssl rand -base64 32", `BETTER_AUTH_URL=http://localhost:3000`, Google optional — "the path to Gmail scopes in Phase 3") (:8-18); LLM providers (any subset; "There is no vendor default in code") + compat slot (:20-30); model per purpose with the **price sheet checked 2026-09-17** and tier guidance ("bulk work (score, extract, verify) on the cheap tier; judgment work (evaluate, tailor) on the strong tier") and "`extract` and `merge` need a vision-capable model" (:32-59); reasoning effort with the measured rationale (92% of DeepSeek Flash scoring output tokens are reasoning; ~$1.50 → $0.24 per 500 postings when off) and `LLM_REASONING_SCORE=none` set (:61-72); RPM (`LLM_RPM_OPENAI=500` set; others default 300) (:74-82); job sources (`RAPIDAPI_KEY`, `JSEARCH_ENABLED=false`, budget comment: free tier 200/month, each result page bills one) (:84-92); Brave `SEARCH_API_KEY` (:94-97); runtime `STORAGE_DIR=./data`, `WORKER_CONCURRENCY=4`, `PDF_BROWSER_CHANNEL` commented (:99-107).

### 6.3 `src/lib/load-dotenv.ts` (14 lines)
`process.loadEnvFile()` in try/catch (:10-14) — loads `./.env` from **cwd** only (not `.env.local` etc.). Imported for side effect by the worker, drizzle-kit config, scripts, `migrate.ts`, and the test setup; **Next does not import it** (Next loads `.env*` itself with its own precedence). Missing file is fine (containers pass real env). Verified semantics (Node 26 here): **existing environment variables win over `.env`**; `FOO=` yields `""` which env.ts then treats as unset. Python: `python-dotenv` `load_dotenv(override=False)` matches.

### 6.4 `drizzle.config.ts` (15 lines)
Loads dotenv, `schema: ./src/lib/db/schema/index.ts`, `out: ./drizzle`, `dialect: postgresql`, **`casing: "snake_case"`**, `dbCredentials.url = env.DATABASE_URL`, `strict`, `verbose`.

---

## 7. Containers

### 7.1 `Dockerfile` (48 lines) — one dependency layer, two prod targets
* `ARG NODE_VERSION=24`, `ARG PLAYWRIGHT_VERSION=1.63.0` (:6-7) — must equal `playwright-core` in package.json.
* `base` (:10-13): `node:24-bookworm-slim`, `PNPM_HOME=/pnpm`, `NEXT_TELEMETRY_DISABLED=1`, `corepack prepare pnpm@10 --activate`, `WORKDIR /app`.
* `deps` (:15-17): copy `package.json pnpm-lock.yaml`, `pnpm install --frozen-lockfile` with BuildKit cache mount `id=pnpm,target=/pnpm/store`.
* `build` (:20-24): `COPY . .`, `ENV SKIP_ENV_VALIDATION=1` ("env.ts validation is skipped at build time; the running container validates for real"), `pnpm build`.
* `web` (:27-37): fresh `node:24-bookworm-slim`; `NODE_ENV=production PORT=3000 HOSTNAME=0.0.0.0`; system user `foray` uid 1001; copies `.next/standalone` → `/app`, `.next/static` → `/app/.next/static`, `public`; `mkdir /app/data` owned by foray; `USER foray`; `EXPOSE 3000`; `CMD ["node","server.js"]`.
* `worker` (:40-48): `mcr.microsoft.com/playwright:v1.63.0-noble` (Chromium + deps preinstalled); `NODE_ENV=production`; corepack pnpm; **`pnpm install --frozen-lockfile` with `NODE_ENV=production` already set** — pnpm skips devDependencies under that setting, and `tsx` is a devDependency while `CMD ["node_modules/.bin/tsx","src/worker/main.ts"]` needs it (possible latent bug; the commit message for Step 2 only claims both targets *build*); `COPY . .`; `mkdir -p /app/data`; runs as root.
* Implication: the `worker` image is also the `migrate` image (compose).

### 7.2 `docker-compose.yml` (80 lines), project name `foray`
* `db` (always on) (:7-26): `pgvector/pgvector:pg18` (pgvector unused in Phase 1; "enabling embeddings later is a migration, not a container swap"); `POSTGRES_USER/PASSWORD/DB = foray`; port `5432:5432`; volume `db-data:/var/lib/postgresql` (**PG18 keeps data in a major-version subdir; mounting the old `/var/lib/postgresql/data` makes the container refuse to start**); healthcheck `pg_isready -U foray -d foray`, interval 5s, timeout 5s, retries 20, start_period 10s.
* `migrate` (profile prod) (:28-40): build target `worker`, `command: node_modules/.bin/tsx src/lib/db/migrate.ts`, `env_file: .env`, `DATABASE_URL=postgres://foray:foray@db:5432/foray`, depends on db healthy, `restart: "no"`.
* `web` (profile prod) (:42-60): target `web`, env_file, DATABASE_URL override, `HOSTNAME 0.0.0.0`, `PORT 3000`, `3000:3000`, volume `storage-data:/app/data`, depends on db healthy and migrate `service_completed_successfully`.
* `worker` (profile prod) (:62-76): target `worker`, env_file, DATABASE_URL override, volume `storage-data:/app/data`, same depends_on. No ports.
* Volumes: `db-data`, `storage-data` (:78-80). **The storage volume is shared by web (uploads, serving PDFs) and worker (writing rendered PDFs).**
* Dev: only `db`; app and worker run natively (`pnpm dev:all`) (compose header :1-2).

### 7.3 `.dockerignore` (16 lines)
Excludes `.git .github node_modules .next out dist data coverage test-results playwright-report .env .env.* *.log .DS_Store .claude`, keeps `.env.example`. Python additions needed: `__pycache__`, `*.pyc`, `.venv`, `.pytest_cache`, `staticfiles`.

---

## 8. Dev tooling and repo config

* **`.claude/launch.json`** (11 lines): one configuration `foray-web` → `pnpm dev`, port 3000. CLAUDE.md requires UI verification in the browser pane via `preview_start foray-web`; after the port add configurations for Django (e.g. `foray-api`, port 8000) and keep the browser hitting Next on :3000 (which must proxy `/api/*` to Django), plus a worker process started separately.
* **`next.config.ts`** (13 lines): `output: "standalone"` (for the web image) (:5); `serverExternalPackages: ["@napi-rs/canvas"]` (native PDF-page rasteriser for the resume vision fallback) (:10). No rewrites/headers today. After the port: drop `serverExternalPackages` (rasterising moves to Python) and add `rewrites()` (or a reverse proxy) for `/api/:path*` (and any auth paths) → Django, so cookies stay first-party.
* **`src/proxy.ts`** (Next 16's renamed "middleware"; 33 lines): optimistic redirect only — if no Better Auth session cookie (`getSessionCookie`, cookie `better-auth.session_token`) on matched paths (`/home /matches /chat /onboarding /pipeline /jobs /activity /profile /companies /settings` + subpaths) → redirect to `/sign-in?next=<path>` (:12-18, 20-33). "It cannot and does not validate the session" (:4-11). If Django changes the cookie name, this check must change with it.
* **`eslint.config.mjs`** (93 lines): next core-web-vitals + typescript configs; global ignores (:10-16). Boundaries (DESIGN invariant 11):
  * `src/app/**`, `src/components/**` may not import `playwright`, `playwright-core`, `playwright/*` ("Playwright belongs to the worker process") nor `@/worker`, `@/worker/*`, `**/worker/*` (:21-42).
  * `src/worker/**` may not import `@/app*`, `@/components*`, `**/app/*` nor `next`, `next/*`, `server-only` ("The worker is a plain Node process") (:43-70).
  * Vendored `src/components/ui/**` and `src/components/ai-elements/**` get `react-hooks/refs` and `react-hooks/static-components` off and unused-vars as warn with `^_` ignore (:77-90).
  * After the port the meaningful frontend boundary becomes: **frontend must not import server modules** (`@/lib/db`, `@/env`, `@/lib/llm/*`, `@/lib/queue/*`, …) — today server components and actions import them freely.
* **`tsconfig.json`**: ES2022, `moduleResolution: bundler`, `strict`, `noEmit`, `allowJs`, `jsx: react-jsx`, Next plugin, `paths {"@/*": ["./src/*"]}`, includes all `**/*.ts(x)`/`.mts` (so `scripts/` and `.cx.ts` are type-checked).
* **`components.json`**: shadcn `style: "base-nova"` (Base UI flavour), RSC, css `src/app/globals.css`, baseColor neutral, CSS variables, lucide icons, aliases (`@/components`, `@/lib/utils`, `@/components/ui`, `@/lib`, `@/hooks`). Frontend-only.
* **`postcss.config.mjs`**: `@tailwindcss/postcss` (Tailwind 4). Frontend-only.
* **`pnpm-workspace.yaml`**: `ignoredBuiltDependencies: sharp, unrs-resolver`; `onlyBuiltDependencies: esbuild`.
* **`.nvmrc`**: `24`.
* **`.gitignore`**: node/next/build/env/`data/`/test output/editor entries; already ignores `__pycache__/`, `*.pyc`, `.venv/` ("python prototype leftovers (see git tag python-prototype)") (:36-39); `/.*.ts` scratch scripts (:41-42).
* **`public/robots.txt`**: `Disallow: /` for all agents.
* **`skills-lock.json`, `.agents/skills/{ai-sdk,migrate-ai-sdk-v6-to-v7}/SKILL.md`, `.claude/skills/*` (symlinks)**: vendored Vercel AI SDK agent skills; irrelevant to Python but still relevant to whatever chat UI code stays in Next (`@ai-sdk/react` `useChat`).
* **`README.md`** (73 lines): product overview; page table (Home, Chat, Matches, Match page, Activity, Profile, Companies, Settings) (:17-26); quickstart requires Node 24+, pnpm 10, Docker Desktop, **Google Chrome** (headless PDF rendering in dev) (:30-31) and the sequence `pnpm install → cp .env.example .env → db:up → db:migrate → seed:companies → dev:all` (:33-40); "Worker offline" sidebar hint (:44-45); cost notes (scoring < 1¢/posting; deep review ≈ 2.5¢; search with 15 reviews ≈ 40¢; tailoring a few to ten cents) (:54-57); commands table (:59-68); status Phase 1 complete, Phase 2 browser apply, Phase 3 Gmail outreach (:70-73). Needs rewriting for the new stack.
* **`AGENTS.md`** (9 lines): Next's auto-written block ("This is NOT the Next.js you know"; read `node_modules/next/dist/docs/`; `next dev` re-adds this block). Stays with the frontend.

---

## 9. `src/lib` purity census and frontend usage

"Pure" = no DB, network or LLM at runtime (may import zod or `node:crypto`). Frontend = `src/app/**`,
`src/components/**` (server components and server actions included; **client** = files with `"use client"`).

### 9.1 Fully pure modules

| Module | Imports | Frontend use | Backend use | Port decision |
|---|---|---|---|---|
| `lib/format.ts` (198) | none | **heavy**: client components (worker-panel, live-panels, tool-part, run-list, user-menu, behaviour-form, match-controls, match-table, tailor-panel, board, run-card, approval-card, company-table, primitives…) and server pages (settings, home, matches, match detail, profile, companies); `actions/companies.ts` uses `plural` in server-action messages (:13, 86, 138, 176) | none in `src/lib`/worker | **stays frontend** (+ its test). Django needs a `plural()` with thousands separators for the messages that move from `actions/companies.ts`. |
| `lib/utils.ts` (1) | re-exports `cn` from npm `cn` | ~30 components | none | frontend only |
| `lib/theme.ts` (81) | none; uses `localStorage`, `matchMedia`, `document` | layout.tsx (THEME_SCRIPT), toaster, theme-sync, appearance-form, user-menu | none | frontend only. Constants: `THEME_STORAGE_KEY "foray-theme"`, default `"dark"`, event `"foray:theme"`, colours light `#ecefea` dark `#0d1310` |
| `lib/chat/follow-up-kinds.ts` (22) | none (deliberately, so clients can import it) | client `chat.tsx`, `live-provider.tsx` | `runs/events.ts` | **shared contract**: `FOLLOW_UP_KINDS = {approval.requested, run.completed, run.failed, tailor.done, tailor.failed}`, `FOLLOW_UP_EVENT = "chat.followup"` — duplicate in Python and TS |
| `lib/pool.ts` (27) | none | none | fast-score, evaluate-run | backend (asyncio semaphore / bounded executor), default concurrency 6, keeps input order |
| `lib/load-dotenv.ts` (14) | none | none | worker, scripts, tests, drizzle config | backend (`python-dotenv`) |
| `lib/queue/queues.ts` (110) | none | server actions `ping`, `runs`, `companies` | everywhere | backend-internal after port (queue names + payload shapes) |
| `lib/llm/pricing.ts` (161) | none | settings page (server component) `rateFor` | deep-eval, ledger, scripts | backend; settings page gets prices via API |
| `lib/sources/clean.ts` (218) | none | none | adapters, careers-page, tailor | backend |
| `lib/sources/normalize.ts` (290) | type-only | none | adapters | backend |
| `lib/sources/types.ts` (90) | type-only (db enums) | none | adapters | backend (`BoardNotFoundError(source, slug)` message `No <source> board for slug "<slug>"`) |
| `lib/sources/dedupe.ts` (119) | `node:crypto` (sha1) | none | adapters | backend (`hashlib.sha1`) |
| `lib/resume/schema.ts` (209) | zod | types only in client (`Resume`, `SkillItem`); runtime `ResumeSchema` in `actions/profile.ts` and `api/resumes/[id]/preview/route.ts` | everywhere | backend (Pydantic); frontend needs TS types (generate from OpenAPI/JSON Schema) |
| `lib/resume/text.ts` (117) | type-only | none | prompts | backend |
| `lib/resume/edits.ts` (81) | type-only | `actions/profile.ts` (server action) | — | backend |
| `lib/resume/render/template.ts` (513) | type-only | `api/resumes/[id]/preview/route.ts` | worker PDF render | backend |
| `lib/resume/parse/garble.ts` (96) | none | none | resume parse | backend |
| `lib/ranking/schemas.ts` (111) | zod | none | ranking | backend |
| `lib/ranking/brief.ts` (174) | zod + interview | activity page (server) `describeBrief/parseBrief`; `actions/runs.ts` | search pipeline, follow-up, live | backend; activity page reads described briefs from API |
| `lib/profile/interview.ts` (237) | zod | **client** `preferences-form.tsx` uses runtime `SLOTS` (+types); `actions/profile.ts` | agents, search | backend + **frontend copy of `SLOTS`** (or serve via API) |
| `lib/agents/system-prompt.ts` (151) | interview + type Settings | none | chat agent | backend |
| `lib/llm/context.ts` (26) | `node:async_hooks` | `api/chat/route.ts` | ledger attribution | backend (`contextvars`) |
| `lib/auth/client.ts` (11) | `better-auth/react` | client sign-in-form, sign-out-button, user-menu | — | frontend only; must be re-pointed at Django auth endpoints (same origin, no baseURL) |

### 9.2 Mixed modules whose *pure exports* are what the unit tests pin
These import `db`/`env`/LLM but the tested functions are pure; port them as plain Python functions so the
pytest versions need no DB: `ranking/prefilter.ts` (titleMatches, guardSeniority, escapeForRegex,
withoutLevel, companyNamePattern, BAY_AREA_PLACES); `ranking/deep-eval.ts` (evalInstructions, evalPrompt,
sanitizeDeepEval, stripFactIds, estimateEvalCost[For], costPhrase); `resume/tailor.ts` (mapTailoredOutput,
evaluationNotes, buildTailorPrompt, TailorOutputSchema); `resume/verify.ts` (all rule/extractor functions;
`verifyTailored` with an injectable `judge`); `companies/discovery.ts` (findAtsLinks, findCareersLinks,
hasUnsupportedAts, normalizeDomain); `companies/verify.ts` (slugCandidates); `sources/jsearch.ts`
(normalizeJSearchJob, atsFromApplyOptions, employerSlug); `sources/{greenhouse,lever,ashby}.ts`
(normalize*); `chat/follow-up.ts` (describeUpdate); `runs/evaluate-run.ts` (summarizeSearchRun,
describeEvaluations); `runs/approvals.ts` (resolveDecision, isApproved, approvedItemIds);
`db/queries/matches.ts` (isShortlistCandidate); `llm/ledger.ts` (tokensFromProviderUsage);
`ranking/bakeoff-fixture.ts` (scoreAgreement).

### 9.3 Server modules the frontend imports today (must become API calls)
Client components import only **types** from `runs/live` (`LiveEvent/LiveRun/LiveApproval/LiveSnapshot`),
`system/heartbeat` (`WorkerStatus`), `db/queries/matches` (`MatchRow`), `agents/chat-agent`
(`ForayUIMessage`), `profile/settings` (`Settings`, `AutonomyMode`), `companies/discovery`
(`DiscoveryStep`) — these become TS types mirroring Django JSON responses. Server components / actions /
route handlers import runtime server code: `db/schema`, `runs/live`, `runs/approvals`, `llm/models`,
`ranking/deep-eval`, `profile/settings`, `queue/queues`, `agents/chat-agent`, `llm/context`, `@/env`
(sign-in page) — all of that moves behind Django endpoints.

---

## 10. What the new dev/prod topology must provide

**Postgres**: keep `pgvector/pgvector:pg18`, same credentials, `db-data:/var/lib/postgresql`, healthcheck.
Existing schema (16+2 tables, snake_case, Better Auth tables `user/session/account/verification`, the
`pgboss` schema, `drizzle.__drizzle_migrations`) must be adopted, not recreated.

**Django API process** (new): ASGI server (uvicorn/daphne/gunicorn+uvicorn workers) because `/api/chat`
streams the AI SDK UI-message stream consumed by `useChat`; serves uploads, resume preview HTML, document
downloads from `STORAGE_DIR`; validates env at boot; DB pool per process (today max 10 prod / 5 dev).
Dev: autoreload on :8000.

**Python worker process** (new): replaces pg-boss handlers. Must provide: per-queue concurrency
(`ping 1, company.discover 2, company.refresh 1, search/search.evaluate/search.shortlist/match.evaluate/
resume.tailor = heavy (1..4), resume.render 2, chat.follow_up 2`, worker/main.ts:50-65); per-queue retry
limit and expiry (`ping 0/1m, company.discover 1/15m, company.refresh 0/60m, search 0/60m, search.evaluate
0/60m, search.shortlist 0/5m, match.evaluate 0/30m, resume.tailor 0/30m, resume.render 1/10m,
chat.follow_up 0/5m`, queue/boss.ts:43-56); immediate wake-up on enqueue (LISTEN/NOTIFY today);
`singletonKey` de-dup (follow-ups); a cron `0 */6 * * *` company refresh with "missed: once" catch-up
(main.ts:119-122); reaper of `running` runs at boot before taking work (main.ts:103-107); heartbeat to
`system_state` every 10 s (main.ts:126-131); graceful drain on SIGINT/SIGTERM with 30 s timeout
(main.ts:136-153). No Redis (settled decision) → a Postgres-backed queue (e.g. Procrastinate) or a
hand-rolled table queue. Dev: restart on save (the reaper then fails interrupted runs — same caveat as today).

**Headless Chromium** for HTML→PDF in the worker: Playwright for Python; prod base image
`mcr.microsoft.com/playwright/python:v<ver>-noble`; dev fallback to installed Google Chrome
(`channel="chrome"`), `PDF_BROWSER_CHANNEL` override; 30 s render timeout (worker/pdf.ts:27); one shared
browser, a context per render; Playwright's own signal handlers disabled so the worker owns shutdown.

**Next.js frontend**: standalone image unchanged in shape (non-root uid 1001, port 3000); no DB, no env
secrets, no native modules; same-origin routing of `/api/*` (and auth endpoints) to Django via
`next.config.ts` rewrites in dev and either rewrites or a reverse proxy in prod. `src/proxy.ts` cookie check
must match Django's session cookie.

**Shared storage**: `STORAGE_DIR` volume mounted in Django (uploads, serving) **and** worker (rendered PDFs).

**Migrate one-shot**: `python manage.py migrate` service, `restart: "no"`, gating api/worker/web via
`service_completed_successfully`.

**Dev orchestration**: `db:up` (unchanged), a `dev:all` running next + django + worker, `.claude/launch.json`
entries for the new processes, the dev-session management command, and the seed command. CI/test: Postgres
service + `pytest` (pytest-django with `--reuse-db` or the dev DB, as today), plus frontend `vitest`.

**Build-time config skip**: equivalent of `SKIP_ENV_VALIDATION=1` so image builds (`collectstatic`,
`next build`) do not need secrets.
