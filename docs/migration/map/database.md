# Foray server-side map: DATABASE

Scope: the full data model (`src/lib/db/schema/**`), the Drizzle migrations (`drizzle/*.sql`),
connection/migration plumbing (`src/lib/db/index.ts`, `src/lib/db/migrate.ts`), the
concurrency helper `src/lib/pool.ts`, every query module in `src/lib/db/queries/**`, all
raw SQL / direct `db` access elsewhere in `src/` and `scripts/`, and the pg-boss
(`pgboss` schema) footprint. All paths are relative to the repo root
`/Users/haydenbarolette/Projects/JobSearchAgent/.claude/worktrees/middleware-django-migration-389b1a`.

Live database facts were gathered read-only (`SET default_transaction_read_only = on`,
`pg_dump --schema-only`, `\d`, `SELECT count(*)`) against the dev container `foray-db-1`
on 2026-10-04.

---

## 0. TL;DR for the Django engineer

* One Postgres 18 database (`foray`), image `pgvector/pgvector:pg18` but **no extension
  is installed** (only `plpgsql`); pgvector is unused (`docker-compose.yml` comment).
  `gen_random_uuid()` is core PG13+.
* Three schemas: `public` (18 app tables, 17 enum types, 1 sequence), `drizzle`
  (`__drizzle_migrations`, 4 rows), `pgboss` (pg-boss 12 queue, schema version 42).
* No triggers, no functions, no views, no CHECK constraints in `public`. Every
  `updated_at` is maintained by application code (or Drizzle `$onUpdate` on the 4
  Better Auth tables). Every invariant beyond NOT NULL / FK / UNIQUE lives in TS.
* Live schema == migration SQL == Drizzle snapshot `drizzle/meta/0003_snapshot.json` ==
  schema TS files (verified: 195 columns identical, 56 index/unique/FK names identical,
  column types/defaults identical; see section 4).
* Enums are real Postgres enum types (`CREATE TYPE ... AS ENUM`), text PKs exist
  (`user.id`, `jobs.id`, `messages.id`, `api_usage.id`, `system_state.key`, Better Auth
  ids), arrays (`text[]`), `jsonb` everywhere, an unbounded `numeric`, a
  `numeric(12,6)`, `timestamp without time zone` on the 4 auth tables and
  `timestamptz` everywhere else, a partial unique index, a partial index, an expression
  GIN index, and `DESC NULLS LAST` btree indexes. Each needs deliberate handling in
  Django (section 11).
* Row counts (dev): jobs 32,115 (647 MB, 98% of DB size 659 MB), llm_calls 1,404,
  job_matches 284, companies 227, session 16, agent_events 32, the rest 0-2.

---

## 1. Connection, migration and helper plumbing

### 1.1 `src/lib/db/index.ts` (whole file, 33 lines)

* `new Pool({ connectionString: env.DATABASE_URL, max: env.NODE_ENV === "production" ? 10 : 5 })`
  (`index.ts:20-25`). Driver: `pg` ^8.23 (node-postgres). ORM: `drizzle-orm` ^0.45.2
  (`drizzle(pool, { schema, casing: "snake_case" })`, `index.ts:29`).
* Hot-reload guard: the pool is cached on `globalThis.__forayPool` when
  `NODE_ENV !== "production"` (`index.ts:16-27`). Why (comment `index.ts:7-14`): Next dev
  re-evaluates modules on every reload; without the cache Postgres runs out of
  connections. The worker imports the same module and gets its own single pool.
* Exports `db`, `pool`, `schema` (namespace), `type Db` (`index.ts:29-33`).
* No relational query API (`db.query.*`) is used anywhere; no `relations()` are defined.
* Type mapping through node-postgres + Drizzle that callers depend on:
  * `numeric` columns come back as **strings** (`jobs.salary_min/max`, `llm_calls.cost_usd`);
    queries that need numbers cast in SQL (`::float8`, e.g. `matches.ts:76-77`,
    `spend.ts:17-20`). Writers pass strings (`ingest.ts:91-92`, `ledger.ts:78 toFixed(6)`).
  * `bigserial` `agent_events.id` uses `mode: "number"` (`runs.ts:80`) -> JS number.
  * `timestamp`/`timestamptz` -> JS `Date`. Drizzle writes `Date` as ISO-8601 UTC; for the
    `timestamp without time zone` auth columns this stores **UTC wall-clock** values
    (confirmed live: `user.created_at 2026-09-18 21:32:00.754` vs migration at 21:28 UTC;
    container `TimeZone = Etc/UTC`; host is PDT). Reads treat them as UTC.
  * `jsonb` -> parsed JS values; `text[]` -> `string[]`.
* Env: `DATABASE_URL` (`src/env.ts:31`, `z.url()`, required). Dev value
  `postgres://foray:***@localhost:5432/foray` (main checkout `.env:6`); compose services use
  `postgres://foray:foray@db:5432/foray` (`docker-compose.yml`).

### 1.2 `src/lib/db/migrate.ts` (whole file, 23 lines)

* Imports `../load-dotenv` (which calls `process.loadEnvFile()`, ignoring a missing file,
  `src/lib/load-dotenv.ts:10-14`), then `migrate(db, { migrationsFolder: "./drizzle" })`
  (`migrate.ts:13`), logs `[migrate] applying migrations from ./drizzle` /
  `[migrate] up to date`, ends the pool, exits 1 on error (`migrate.ts:17-23`).
* Run by `pnpm db:migrate` (`package.json` `"db:migrate": "tsx src/lib/db/migrate.ts"`) and
  by the one-shot `migrate` compose service (`docker-compose.yml`, profile `prod`,
  `command: ['node_modules/.bin/tsx', 'src/lib/db/migrate.ts']`); `web` and `worker`
  `depends_on: migrate: service_completed_successfully`.
* Drizzle bookkeeping table `drizzle.__drizzle_migrations(id serial pk, hash text not null,
  created_at bigint)`; live rows (id, created_at ms):
  1 `1789766915176`, 2 `1789767089836`, 3 `1789775666421`, 4 `1790295961902` (match
  `drizzle/meta/_journal.json` `when` values).
* `drizzle.config.ts`: `schema: ./src/lib/db/schema/index.ts`, `out: ./drizzle`,
  `dialect: postgresql`, `casing: snake_case`, `strict: true`, `verbose: true`.
* Other DB scripts in `package.json`: `db:generate` (drizzle-kit generate), `db:studio`,
  `db:up` / `db:down` (docker compose), `auth:generate` (`better-auth generate --config
  src/lib/auth/index.ts` - regenerates auth tables into the Drizzle schema).

### 1.3 `src/lib/pool.ts` (whole file, 27 lines) - NOT a DB pool

`pooled<T, R>(items: T[], worker: (item, index) => Promise<R>, concurrency = 6): Promise<R[]>`
(`pool.ts:8-27`). Runs `min(concurrency, items.length)` async loops pulling from a shared
cursor; results are written at the input index so **output order == input order**
(comment `pool.ts:1-7`). No error handling: the first rejection rejects the whole
`Promise.all` (other loops keep running). Consumers: `src/lib/ranking/fast-score.ts:172`
(`CONCURRENCY = 16`, `fast-score.ts:39`) and `src/lib/runs/evaluate-run.ts:703`
(`EVAL_CONCURRENCY = 5`, `evaluate-run.ts:67`). `src/lib/companies/refresh.ts:97-115`
re-implements the same pattern inline (default 6). Python: `asyncio.Semaphore` +
`gather` (preserves order), or a `ThreadPoolExecutor.map`.

---

## 2. Enums (`src/lib/db/schema/enums.ts`, all real PG enum types, created in `drizzle/0000_calm_bloodscream.sql:1-17`)

Comment `enums.ts:3-6`: each is a real Postgres enum "so a typo is a write error rather
than a row nobody notices".

| PG type | Members (order matters for enum comparison) | Defined | Notes on actual use |
|---|---|---|---|
| `job_source` | greenhouse, lever, ashby, jsearch, web | `enums.ts:9-15` | `web` = LLM careers-page extractor. All 5 present in live data. |
| `company_source_type` | greenhouse, lever, ashby, careers_page, none | `enums.ts:18-24` | `none` = registered but unfetchable; never written by current code. |
| `company_status` | active, invalid, paused | `enums.ts:26-30` | `paused` never written. |
| `discovered_via` | seed, agent, jsearch, user | `enums.ts:32-37` | seed (`scripts/seed-companies.ts:47`), agent (`discovery.ts:387`, `worker/handlers/company.ts:29`), jsearch (`discovery.ts:438`), user (`app/actions/companies.ts:49`, `agents/tools/companies.ts:33`, `runs/search-run.ts:495`). |
| `remote_kind` | remote, hybrid, onsite, unknown | `enums.ts:39-44` | |
| `match_stage` | found, shortlisted, applied, interviewing, offer, rejected, archived | `enums.ts:47-55` | OPEN = first 5, CLOSED = rejected, archived (`queries/matches.ts:17-25`). Cast literally in SQL: `'archived'::match_stage` (`matches.ts:544`). |
| `run_kind` | search, rank_deep, tailor, resume_render, company_discovery, company_refresh, profile_import | `enums.ts:61-69` | Only search, rank_deep, tailor, resume_render are ever inserted. company_* / profile_import are only referenced by UI switches (`app/(app)/activity/page.tsx:148,152`, `components/activity/run-summary.ts:82`). Comment `enums.ts:57-60`: later phases add members here (apply, send_email, browser_connect) rather than new tables. |
| `run_mode` | approve_each, autopilot | `enums.ts:72` | default approve_each |
| `run_status` | queued, running, awaiting_approval, completed, failed, cancelled | `enums.ts:74-81` | ACTIVE = queued, running, awaiting_approval (`runs/runs.ts:145`, `approvals.ts:593-597`). |
| `event_level` | debug, info, warn, error | `enums.ts:83-88` | |
| `approval_kind` | deep_eval_batch, shortlist_proposal, tailor, company_add | `enums.ts:90-95` | tailor / company_add never written. |
| `approval_status` | pending, approved, rejected, expired, auto_approved | `enums.ts:97-103` | |
| `decided_via` | ui, chat, autopilot | `enums.ts:105` | |
| `document_kind` | resume_upload, linkedin_pdf, resume_pdf | `enums.ts:108-112` | Uploads + renders share one table. |
| `resume_kind` | base, tailored | `enums.ts:114` | |
| `render_engine` | html, latex | `enums.ts:117` | latex never written (`markRendered` default "html"). |
| `llm_purpose` | chat, extract, score, evaluate, tailor, verify, merge | `enums.ts:123-131` | Comment `enums.ts:119-122`: call sites ask for a purpose; models.ts maps to provider/model. |

pg-boss adds its own enum `pgboss.job_state` = created, retry, active, completed, cancelled, failed.

---

## 3. Tables (public schema)

Conventions across all tables:
* `uuid` PKs default `gen_random_uuid()`; Drizzle `.defaultRandom()`. Some code supplies
  its own uuid (`approvals.ts:145 randomUUID()`, `ensureConversation` claims a client id).
* `created_at`/`updated_at` default `now()`, NOT NULL. `updated_at` is **set explicitly
  by every app-code UPDATE** (`updatedAt: new Date()`) - there is no trigger. Auth tables
  use Drizzle `$onUpdate(() => new Date())` (`auth.ts:30-33` etc.), which only fires for
  updates issued through Drizzle (Better Auth's adapter).
* FK names follow `<table>_<col>_<reftable>_<refcol>_fk`, all `ON UPDATE NO ACTION`.
* Per-user tables carry `user_id text NOT NULL REFERENCES "user"(id) ON DELETE CASCADE`
  except `llm_calls.user_id` and `companies.added_by_user_id` (SET NULL). Deleting a user
  wipes every per-user row; tests rely on that (`src/test/unit/approvals.test.ts:37-43`).

### 3.1 Better Auth tables (`src/lib/db/schema/auth.ts`)

Comment `auth.ts:9-21`: Better Auth owns the shape (`pnpm auth:generate` regenerates
it); "our own columns go on `profiles`, not on `user`". Differences from the generator:
snake_case index names and `defaultNow()` on session/account `updated_at`.
Written by Better Auth 1.7.5 via `drizzleAdapter(db, { provider: "pg", schema: { user,
session, account, verification } })` (`src/lib/auth/index.ts:29-32`), email+password
enabled, `requireEmailVerification: false`, optional Google social provider
(`auth/index.ts:15-23,34-41`). **All timestamps here are `timestamp without time zone`**
holding UTC wall-clock values.

#### `user` (`auth.ts:23-34`; SQL `0000:46-55`) - live rows: 1
| column | type | null | default | notes |
|---|---|---|---|---|
| id | text | NO | - | PK. Better Auth generated id (32 chars, e.g. used as storage path prefix). |
| name | text | NO | - | |
| email | text | NO | - | UNIQUE `user_email_unique` |
| email_verified | boolean | NO | false | |
| image | text | YES | - | |
| created_at | timestamp | NO | now() | |
| updated_at | timestamp | NO | now() | `$onUpdate` |

Reserved word: the table is literally named `user` (quoted in SQL).

#### `session` (`auth.ts:36-54`; SQL `0000:34-44`) - live rows: 16
| column | type | null | default | notes |
|---|---|---|---|---|
| id | text | NO | | PK |
| expires_at | timestamp | NO | | live TTL = 7 days |
| token | text | NO | | UNIQUE `session_token_unique` |
| created_at | timestamp | NO | now() | |
| updated_at | timestamp | NO | now() | `$onUpdate` |
| ip_address | text | YES | | |
| user_agent | text | YES | | `"dev-session script"` for minted sessions |
| user_id | text | NO | | FK -> user.id ON DELETE CASCADE |
Index `session_user_id_idx (user_id)`.

Cookie contract (`scripts/dev-session.ts:34-48`): cookie name `better-auth.session_token`,
value `encodeURIComponent(token + "." + base64(HMAC-SHA256(BETTER_AUTH_SECRET, token)))`;
token = `randomBytes(24).toString("base64url")`, id = `randomBytes(16).hex`, expires 7 days.

#### `account` (`auth.ts:56-79`; SQL `0000:18-32`) - live rows: 1 (`provider_id = 'credential'`)
| column | type | null | notes |
|---|---|---|---|
| id | text | NO | PK |
| account_id | text | NO | |
| provider_id | text | NO | `credential` for email+password, `google` for OAuth |
| user_id | text | NO | FK -> user.id CASCADE |
| access_token, refresh_token, id_token | text | YES | |
| access_token_expires_at, refresh_token_expires_at | timestamp | YES | |
| scope | text | YES | |
| password | text | YES | live length 161 = Better Auth scrypt `"<32 hex salt>:<128 hex key>"` |
| created_at, updated_at | timestamp | NO, now() | `$onUpdate` on updated_at |
Index `account_user_id_idx (user_id)`. A unique index `account_provider_account_idx
(provider_id, account_id)` was created in `0000:278` and **dropped** in
`drizzle/0001_wealthy_human_cannonball.sql` (single statement, no trailing newline), so
today nothing prevents duplicate (provider, account_id) rows.

#### `verification` (`auth.ts:81-95`; SQL `0000:57-64`) - live rows: 0
id text PK; identifier text NN; value text NN; expires_at timestamp NN; created_at /
updated_at timestamp NN default now(). Index `verification_identifier_idx (identifier)`.

Scripts that touch auth tables directly: `scripts/dev-session.ts:29-41` (SELECT user by
email or first user; INSERT session; refuses when NODE_ENV=production or no
BETTER_AUTH_SECRET), `scripts/set-password.ts:25-46` (SELECT user by email, then Better
Auth `internalAdapter.findAccounts / updatePassword(ctx.password.hash(...))`).

### 3.2 Shared mirror: `companies`, `jobs` (`src/lib/db/schema/jobs.ts`)

Comment `jobs.ts:25-32`: `companies` and `jobs` are **not per-user**; one fetch serves
everyone; a vanished posting is kept with `is_active = false`.

#### `companies` (`jobs.ts:34-80`; SQL `0000:66-85`) - live rows: 227
| column | type | null | default | notes |
|---|---|---|---|---|
| id | uuid | NO | gen_random_uuid() | PK |
| name | text | NO | | |
| domain | text | YES | | bare registrable domain e.g. `stripe.com` (`jobs.ts:39`) |
| slug | text | NO | | UNIQUE `companies_slug_unique`; "also the middle segment of every job id" (`jobs.ts:41`) |
| source_type | company_source_type | NO | | |
| ats_slug | text | YES | | board id at the ATS; NULL for careers_page/none |
| careers_urls | text[] | NO | `'{}'` | |
| tags | text[] | NO | `'{}'` | live values: hardware, security, enterprise, devtools, consumer, fintech, ai, marketplace, infra, data, crypto, biotech, climate, healthtech |
| status | company_status | NO | 'active' | |
| last_fetched_at | timestamptz | YES | | |
| last_fetch_status | text | YES | | free text; values written: `ok`, `not-found`, `error`, `needs-browser` (`companies/refresh.ts:66,81,139,162`) |
| last_fetch_job_count | integer | YES | | |
| needs_browser | boolean | NO | false | careers page needs JS (Phase 2) |
| discovered_via | discovered_via | NO | | |
| added_by_user_id | text | YES | | FK -> user.id ON DELETE SET NULL; "who asked, not an owner" (`jobs.ts:59`) |
| created_at, updated_at | timestamptz | NO | now() | |
Indexes (`jobs.ts:71-79`; SQL `0000:281-283`):
* `companies_source_ats_slug_idx` UNIQUE btree `(source_type, ats_slug) WHERE ats_slug IS NOT NULL`
  - partial because careers_page/none have NULL ats_slug (`jobs.ts:72-73`).
* `companies_status_idx (status)`; `companies_last_fetched_at_idx (last_fetched_at)`.
Live profile: greenhouse 125 (3 invalid), ashby 91, lever 10, careers_page 1; seed 223, user 4.

#### `jobs` (`jobs.ts:82-153`; SQL `0000:104-132`) - live rows: 32,115 (647 MB)
| column | type | null | default | notes |
|---|---|---|---|---|
| id | text | NO | | PK. `source:company_slug:external_id`; careers pages `web:<slug>:<sha1(url)>` (`jobs.ts:85`, `sources/dedupe.ts:104-119`). Live lengths: greenhouse 23-43, ashby 46-63, lever 45-57, web 51, **jsearch 413-466** chars. |
| source | job_source | NO | | |
| external_id | text | NO | | id at source |
| company_id | uuid | YES | | FK -> companies.id ON DELETE SET NULL; NULL for all jsearch rows |
| company_name | text | NO | | denormalized (aggregator rows arrive before the company is registered, `jobs.ts:94`) |
| title | text | NO | | |
| locations | text[] | NO | `'{}'` | |
| location_norm | jsonb | YES | | `NormalizedLocation` (below) |
| remote | remote_kind | NO | 'unknown' | |
| employment_type | text | YES | | free text (FullTime, Full-time, Intern, CLT, ...) |
| salary_min, salary_max | numeric (unbounded) | YES | | written as strings |
| salary_currency | text | YES | | USD, GBP, EUR, CAD, SGD... |
| salary_period | text | YES | | `year` / `month` / `hour` as the source states, never converted (`jobs.ts:107`) |
| description_raw | text | YES | | **never rewritten** (DESIGN.md invariant 4, `jobs.ts:110`) |
| description_text | text | YES | | cleaned copy; derived, disposable |
| url | text | NO | | |
| apply_url | text | YES | | |
| posted_at | timestamptz | YES | | NULL for web postings |
| raw | jsonb | YES | | source payload, kept to replay parser fixes (always an object live) |
| content_hash | text | YES | | sha1 hex (40) of `JSON.stringify([title, sorted(locations), descriptionRaw ?? "", salaryMin ?? null, salaryMax ?? null])` (`dedupe.ts:83-101`) |
| dedupe_key | text | YES | | sha1 hex of `norm(company)|norm(title)|norm(primary location)` (`dedupe.ts:64-75`) |
| canonical_job_id | text | YES | | "points at the ATS posting when this row is an aggregator duplicate" - **never written by any code** (0 live rows non-null); only read as `IS NULL` filter (`ranking/prefilter.ts:336`) |
| is_active | boolean | NO | true | |
| first_seen_at | timestamptz | NO | now() | never updated after insert |
| last_seen_at | timestamptz | NO | now() | |
| fetched_at | timestamptz | NO | now() | cache metadata (invariant 6) |
Indexes (`jobs.ts:140-152`; SQL `0000:287-291`):
* `jobs_company_id_idx (company_id)`, `jobs_dedupe_key_idx (dedupe_key)`,
  `jobs_canonical_job_id_idx (canonical_job_id)`.
* `jobs_active_posted_at_idx (posted_at DESC NULLS LAST) WHERE is_active` - "the search
  prefilter's shape" (`jobs.ts:144-147`).
* `jobs_fts_idx` GIN on `to_tsvector('english', title || ' ' || coalesce(description_text, ''))`
  (`jobs.ts:148-151`). **No query uses full-text search** (grep for tsvector/tsquery finds
  nothing outside the schema) - dead index, but expensive on a 32k-row 647 MB table.
No `updated_at` column. No unique index on (source, external_id) - id is the identity.

`location_norm` shape = `NormalizedLocation` (`src/lib/sources/types.ts:15-22`):
`{ raw: string; city: string|null; region: string|null; country: string|null; metro: string|null }`
(live keys exactly city,country,metro,raw,region; only metro value in use: `bay-area`).

#### `job_matches` (`jobs.ts:159-199`; SQL `0000:87-102`) - live rows: 284
A user's relationship to one job (`jobs.ts:155-158`).
| column | type | null | default | notes |
|---|---|---|---|---|
| id | uuid | NO | gen_random_uuid() | PK |
| user_id | text | NO | | FK -> user CASCADE |
| job_id | text | NO | | FK -> jobs.id CASCADE |
| stage | match_stage | NO | 'found' | |
| stage_changed_at | timestamptz | NO | now() | |
| fast_score | integer | YES | | 0-100 |
| fast_eval | jsonb | YES | | `FastScore` |
| deep_score | integer | YES | | 0-100 |
| deep_eval | jsonb | YES | | `DeepEval` |
| user_rating | integer | YES | | validated -10..10 by `app/actions/matches.ts:76` |
| notes | text | YES | | free text, appended with `\n\n` separators |
| tailored_resume_id | uuid | YES | | **no FK** (plain uuid); points at `resumes.id` |
| created_at, updated_at | timestamptz | NO | now() | |
Indexes (`jobs.ts:194-198`; SQL `0000:284-286`): UNIQUE `job_matches_user_job_idx (user_id, job_id)`;
`job_matches_user_stage_idx (user_id, stage)`; `job_matches_user_fast_score_idx (user_id, fast_score DESC NULLS LAST)`.

`fast_eval` = zod `FastScore` (`src/lib/ranking/schemas.ts:26-41`): `score` int 0-100,
`fit_reasons` string[], `red_flags` string[], `seniority` enum
intern|new_grad|entry|mid|senior|staff_plus|unclear (`schemas.ts:11-19`),
`hard_requirement_misses` string[].
`deep_eval` = zod `DeepEval` (`schemas.ts:87-109`): `matched_requirements`
[{requirement, evidence, fact_ids: string[], strength: strong|partial}], `gaps`
[{gap, severity: learnable|disqualifying, note}], `seniority_fit` under|match|over|unclear,
`location_fit` match|partial|mismatch|unclear, `salary_fit` above|within|below|unstated,
`risk_flags` string[], `talking_points` string[], `summary` string, `score` int 0-100,
`recommendation` strong|apply|maybe|skip. Field order is intentional (evidence before
verdict, `schemas.ts:73-86`). Live key sets match exactly.

### 3.3 Profile, documents, resumes (`src/lib/db/schema/profile.ts`)

#### `profiles` (`profile.ts:20-68`; SQL `0000:147-157` + `0003:7`) - live rows: 1
| column | type | null | default | notes |
|---|---|---|---|---|
| id | uuid | NO | gen_random_uuid() | PK |
| user_id | text | NO | | UNIQUE `profiles_user_id_unique`; FK -> user CASCADE. One profile per user. |
| data | jsonb | NO | `'{}'` | mirror of latest base resume (`Resume` shape). Invariant 13: only a mirror. |
| preferences | jsonb | NO | `'{}'` | `Preferences` |
| interview_state | jsonb | NO | `'{}'` | `InterviewState` |
| assessment | jsonb | YES | | reserved, **never read or written** (`profile.ts:55-59`) |
| created_at, updated_at | timestamptz | NO | now() | |
| settings | jsonb | NO | `'{}'` | added by `drizzle/0003_settings_and_system_state.sql:7`; physically the **last** column |
No secondary index (the unique constraint indexes user_id). A profile row is created
only by `saveBaseResume` (first resume upload) via upsert (`queries/profile.ts:89-98`);
`updateSettings` and preference writes return "no profile yet" when absent.

JSON shapes:
* `data`: `ResumeSchema` (`src/lib/resume/schema.ts:86-93`): `basics {name (default ""),
  headline?, email?, phone?, location?, links: [{id, source, label, url}]}`, `summary?`,
  `work [{id, source, company, position, location?, startDate?, endDate?, summary?,
  highlights: string[]}]`, `education [{id, source, institution, area?, studyType?,
  startDate?, endDate?, score?, highlights}]`, `projects [{id, source, name, description?,
  url?, startDate?, endDate?, highlights}]`, `skills [{id, source, name, category?}]`.
  `source` enum resume|linkedin_pdf|user default resume (`schema.ts:13,23-27`); dates are
  verbatim strings 1-40 chars (`schema.ts:16-21`). `?` = nullish.
* `preferences`: `PreferencesSchema` (`src/lib/profile/interview.ts:46-56`): every key is
  `nullable().default(null)` (`interview.ts:43-44`): `target_roles string[]`, `seniority`
  intern|new_grad|entry|mid|senior|staff_plus, `locations string[]`, `remote_pref`
  remote|hybrid|onsite|any, `salary_floor {amount: number, currency: string, period:
  year|hour}`, `work_auth string`, `start_date string`, `must_haves string[]`,
  `dealbreakers string[]`. Why default+nullable (`interview.ts:32-42`): null means "not
  asked yet" vs answered; defaults keep rows from older versions parseable so answers are
  never discarded.
* `interview_state`: `InterviewStateSchema` (`interview.ts:165-169`): `{version: number
  (default INTERVIEW_VERSION = 1), skipped: string[] (default [])}`. Live:
  `{"skipped": [], "version": 1}`.
* `settings`: `SettingsSchema` (`src/lib/profile/settings.ts:18-28`): `defaultMode`
  approve_each|autopilot (default approve_each), `scoreThreshold` int 0-100 (default 55),
  `deepEvalTopN` int 0-60 (default 15). `parseSettings` falls back to defaults on any
  parse failure (`settings.ts:34-36`). Live: `{}` (all defaults).

#### `documents` (`profile.ts:71-98`; SQL `0000:134-145`) - live rows: 2
| column | type | null | notes |
|---|---|---|---|
| id | uuid | NO | PK default gen_random_uuid() |
| user_id | text | NO | FK -> user CASCADE |
| kind | document_kind | NO | |
| filename | text | NO | |
| mime | text | NO | |
| size_bytes | integer | NO | |
| storage_path | text | NO | opaque; local storage key `${userId}/${kind}/${Date.now()}-${safeFilename}` (`src/lib/storage/index.ts:33-40`) |
| sha256 | text | NO | 64 hex |
| text_extracted | text | YES | text pulled from the file before any LLM sees it |
| created_at | timestamptz | NO | now() |
Indexes: `documents_user_kind_idx (user_id, kind)`; UNIQUE `documents_user_sha256_idx
(user_id, sha256)` - "the same upload twice is the same document" (`profile.ts:95-96`).
Note the unique key ignores `kind`: identical bytes uploaded as a different kind resolve
to the first row.

#### `resumes` (`profile.ts:100-144`; SQL `0000:159-172`) - live rows: 2
| column | type | null | default | notes |
|---|---|---|---|---|
| id | uuid | NO | gen_random_uuid() | PK |
| user_id | text | NO | | FK -> user CASCADE |
| kind | resume_kind | NO | | |
| job_match_id | uuid | YES | | FK -> job_matches.id **ON DELETE CASCADE**; set on tailored |
| version | integer | NO | 1 | base: per user; tailored: per (user, match). **No unique constraint**. |
| data | jsonb | NO | | `Resume` (same as profiles.data) |
| rendered_document_id | uuid | YES | | FK -> documents.id ON DELETE SET NULL |
| render_engine | render_engine | YES | | |
| changes | jsonb | YES | | `TailorChange[]` |
| verification | jsonb | YES | | `Verification` |
| created_at, updated_at | timestamptz | NO | now() | |
Indexes: `resumes_user_kind_idx (user_id, kind)`, `resumes_job_match_id_idx (job_match_id)`.

* `changes` element = `TailorChange` (`src/lib/resume/tailor.ts:39-50`): `{section:
  TailorSection ("summary"|"work"|"projects"|... `tailor.ts:25-33`), item_id: string|null,
  before: string|null, after: string|null, reason: string, evidence_fact_ids: string[]}`.
* `verification` = `Verification` (`src/lib/resume/verify.ts:49-68`): `{passed: boolean,
  checkedAt: string (ISO), unsupported: UnsupportedClaim[], revised: boolean, checked:
  number, modelChecked: number, revisions: Revision[], modelError: string|null,
  baseResumeId: string|null, baseVersion: number|null}`; `UnsupportedClaim = {section,
  item_id: string|null, text, reason, source: "rule"|"model"}` (`verify.ts:33-39`);
  `Revision = UnsupportedClaim & {action: "replaced"|"dropped"|"reverted", replacement:
  string|null}` (`verify.ts:42-46`).
* Versioning invariant (`queries/profile.ts:64-71,103-111`; DESIGN.md invariant 13): base
  resumes are versioned, never overwritten, because tailored `changes[]` cite base fact ids
  that must keep resolving; the newest base row is the single source of facts.

### 3.4 Runs, events, approvals (`src/lib/db/schema/runs.ts`)

Comment `runs.ts:22-28`: one `runs` table for every run kind so the feed, run card and
cancellation keep working unchanged.

#### `runs` (`runs.ts:30-71`; SQL `0000:202-219`) - live rows: 2
| column | type | null | default | notes |
|---|---|---|---|---|
| id | uuid | NO | gen_random_uuid() | PK |
| user_id | text | NO | | FK -> user CASCADE |
| kind | run_kind | NO | | |
| parent_run_id | uuid | YES | | **no FK; never written** (indexed) |
| mode | run_mode | NO | 'approve_each' | |
| status | run_status | NO | 'queued' | |
| input | jsonb | NO | `'{}'` | per-kind (below); merged with `||` |
| stats | jsonb | NO | `'{}'` | live counters; always shallow-merged with `||` |
| output | jsonb | YES | | |
| error | text | YES | | |
| conversation_id | uuid | YES | | **no FK** to conversations; set when started from chat |
| pgboss_job_id | text | YES | | **never written** (send() return value is discarded) |
| started_at, finished_at | timestamptz | YES | | |
| created_at, updated_at | timestamptz | NO | now() | |
Indexes: `runs_user_created_at_idx (user_id, created_at DESC NULLS LAST)`,
`runs_user_status_idx (user_id, status)`, `runs_parent_run_id_idx (parent_run_id)`.

`input` by kind (writers):
* search: `SearchRunInput` (`src/lib/runs/search-run.ts:87-106`): `prompt?: string|null`,
  `hints?: Partial<SearchBrief>|null`, `brief?: SearchBrief`, `criteria?: DerivedCriteria`,
  `skipRefresh?: boolean`, `candidateJobIds?: string[]`. Created with `{prompt, hints}`
  (`app/actions/runs.ts:68-73`, `agents/tools/search.ts:130`); brief/criteria/
  candidateJobIds merged in later via `saveInput` (`search-run.ts:550-558`). `SearchBrief`
  (`src/lib/ranking/brief.ts:23-33`): roles string[]|null, seniority|null, locations
  string[]|null, remote (remote|hybrid|onsite|any)|null, companies string[]|null, focus
  string|null. `DerivedCriteria` (`ranking/prefilter.ts:37-56`): title_include string[],
  title_exclude string[], keywords string[], title_keep? string[].
* rank_deep: `{matchIds: string[]}` **sorted** (`runs/evaluate-run.ts:528,548`) - sorted so
  jsonb equality can detect a duplicate in-flight run (`evaluate-run.ts:530-542`).
* tailor: `{matchId, prompt: "Tailor your resume for <title> at <company>"}` (`tailor-run.ts:332`).
* resume_render: `{resumeId, prompt: "Render your resume to PDF" | "Render your tailored resume to PDF"}` (`tailor-run.ts:376-382`).
* `toLiveRun` reads `input.prompt`, `input.brief ?? input.hints` (`runs/live.ts:71-94`).
Live `stats` keys for a completed search: activeInMirror, aggregatorJobs, aggregatorRequests,
autoArchived, boardsFailed, boardsRefreshed, candidates, companiesDiscovered, evaluated,
evaluationFailed, matches, newMatches, phase, proposedShortlist, recommended, resumedFrom,
scored, scoringFailed, shortlisted, toEvaluate; `output` keys: autoArchived, candidates,
evaluated, newMatches, scored, shortlisted, topMatchIds. Tailor `stats`: baseVersion,
changes, checked, company, documentId, matchId, pages, phase, resumeId, revisedCount, title,
unsupported; `output`: changes, documentId, matchId, pages, resumeId, unsupported.
`stats.phase` sequence for search: starting -> refreshing -> aggregating -> filtering ->
scoring -> awaiting_evaluation -> evaluating -> awaiting_shortlist -> done
(`search-run.ts:60-68`). `stats.resumedFrom` = approval id used for idempotent resume
(`evaluate-run.ts:818-839`).

#### `agent_events` (`runs.ts:77-103`; SQL `0000:174-184`) - live rows: 32
Append-only progress; `/api/live?since=<id>` polls it, "which is why the id is a monotonic
bigserial rather than a uuid" (`runs.ts:73-76`).
| column | type | null | default | notes |
|---|---|---|---|---|
| id | bigserial (bigint + `agent_events_id_seq`) | NO | nextval | PK; the live-feed cursor |
| user_id | text | NO | | FK -> user CASCADE |
| run_id | uuid | YES | | FK -> runs.id CASCADE |
| level | event_level | NO | 'info' | |
| kind | text | NO | | dotted name |
| message | text | NO | | |
| data | jsonb | YES | | per-kind |
| approval_id | uuid | YES | | **no FK**; set on approval.* events |
| created_at | timestamptz | NO | now() | |
Indexes: `agent_events_user_id_idx (user_id, id)`, `agent_events_run_id_idx (run_id, id)`.
Kinds emitted in code (grep of `kind: "..."`): approval.auto, approval.decided,
approval.expired, approval.reopened, approval.requested, chat.followup
(`chat/follow-up-kinds.ts:22`), company.discovered / company.not-found
(`worker/handlers/company.ts:35`), company.refresh.done, evaluation.done/failed/progress/
skipped/start, prefilter.done, render.done/failed, run.cancelled/completed/failed/started,
scoring.done/progress/start, search.companies.added/missing, search.criteria,
shortlist.done/skipped, source.aggregator.done/skipped, source.refresh.done/start,
tailor.done/failed/started/verified, worker.ping. Data shapes seen live:
approval.decided `{approved,itemIds,kind,via}`, approval.requested `{count,estimate,kind}`,
prefilter.done `{activeInMirror,cappedAt,selected}`, run.completed = run output,
search.criteria `{brief,criteria}`, shortlist.done `{matchIds}`, source.aggregator.done
`{requestsThisMonth}`, tailor.done `{documentId,matchId,resumeId}`, tailor.verified
`{checked,modelChecked,passed,resumeId,revised,unsupported}`, chat.followup
`{conversationId, runId, messageId?, label?, skipped?, failed?}`.
Side effect contract: `emit()` (`src/lib/runs/events.ts:28-50`) enqueues a
`chat.follow_up` job when `run_id` is set and `kind` is in FOLLOW_UP_KINDS
(approval.requested, run.completed, run.failed, tailor.done, tailor.failed;
`chat/follow-up-kinds.ts:10-16`) and the run has a conversation_id.

#### `approvals` (`runs.ts:113-147`; SQL `0000:186-200`) - live rows: 2
Continuation pattern (`runs.ts:105-112`): the handler writes the row with the
continuation and returns; deciding enqueues it; autopilot uses the same path with
`decided_via = 'autopilot'`.
| column | type | null | default | notes |
|---|---|---|---|---|
| id | uuid | NO | gen_random_uuid() | PK; app pre-generates with randomUUID (`approvals.ts:145`) so the continuation can embed it |
| user_id | text | NO | | FK -> user CASCADE |
| run_id | uuid | NO | | FK -> runs CASCADE |
| kind | approval_kind | NO | | |
| title | text | NO | | |
| payload | jsonb | NO | `'{}'` | items + cost estimate |
| status | approval_status | NO | 'pending' | |
| decision | jsonb | YES | | `StoredDecision {approved: boolean, itemIds?: string[]}` (`approvals.ts:51-54`) |
| decided_via | decided_via | YES | | |
| decided_at | timestamptz | YES | | |
| continuation | jsonb | NO | | `{queue: QueueName, payload: QueuePayloads[queue]}` (`approvals.ts:35-37`) - enqueued verbatim |
| expires_at | timestamptz | YES | | pending rows: now + 7 days (`APPROVAL_TTL_MS`, `approvals.ts:29,197`); autopilot rows: NULL |
| created_at | timestamptz | NO | now() | |
No `updated_at`. Indexes: `approvals_user_status_idx (user_id, status)`,
`approvals_run_id_idx (run_id)`.
Payloads (`approvals.ts:58-95`): `deep_eval_batch` = `{items: [{matchId, jobId, title,
company, fastScore}], estimate: {usd: number|null, perItemUsd: number|null, model:
"provider:model"}, note: string|null}`; `shortlist_proposal` = `{items: [{matchId, jobId,
title, company, deepScore, recommendation, summary}]}`. Item identity = `matchId` else
`id` (`payloadItemIds`, `approvals.ts:401-410`). Live continuations: `search.evaluate` /
`search.shortlist` with payload `{userId, runId, approvalId}`.

### 3.5 Chat and ledger (`src/lib/db/schema/chat.ts`)

#### `conversations` (`chat.ts:16-32`; SQL `0000:221-227`) - live rows: 0
id uuid PK default gen_random_uuid() (client may supply it, see `ensureConversation`);
user_id text NN FK -> user CASCADE; title text NULL (first user text sliced to 80 chars,
`app/api/chat/route.ts:41-43`); created_at / updated_at timestamptz NN now().
Index `conversations_user_updated_at_idx (user_id, updated_at DESC NULLS LAST)`.

#### `messages` (`chat.ts:38-65`; SQL `0000:246-254`) - live rows: 0
One AI SDK `UIMessage` per row keyed by the SDK's own id (`chat.ts:34-37`).
| column | type | null | notes |
|---|---|---|---|
| id | text | NO | PK; the AI SDK message id (`generateId()`), **not ours**. Must be non-empty (empty id collapsed all replies onto one row - `api/chat/route.ts:72-78`). |
| conversation_id | uuid | NO | FK -> conversations CASCADE |
| user_id | text | NO | FK -> user CASCADE |
| role | text | NO | `user` / `assistant` / `system` |
| parts | jsonb | NO | `UIMessage.parts` verbatim (text, tool-*, reasoning, file, step-start, ...) |
| metadata | jsonb | YES | `RunUpdateMetadata {kind:"run_update", runId, eventId, label}` or `FollowUpMetadata {kind:"follow_up", runId, eventId}` (`chat/follow-up.ts:47-56`), else whatever the client/SDK sends |
| created_at | timestamptz | NO | now(); ordering key |
Index `messages_conversation_created_at_idx (conversation_id, created_at)`.

#### `llm_calls` (`chat.ts:74-108`; SQL `0000:229-244`) - live rows: 1,404
The cost ledger; "nothing may call a provider without writing a row here" (`chat.ts:67-73`,
DESIGN.md invariant 10).
| column | type | null | default | notes |
|---|---|---|---|---|
| id | uuid | NO | gen_random_uuid() | PK |
| user_id | text | YES | | FK -> user ON DELETE **SET NULL** (rows survive user deletion; 1,135 of 1,404 have a user) |
| run_id | uuid | YES | | FK -> runs ON DELETE SET NULL |
| purpose | llm_purpose | NO | | |
| provider | text | NO | | first segment of `provider:model` ref |
| model | text | NO | | the rest of the ref joined with `:` |
| input_tokens | integer | NO | 0 | uncached input **plus cache-write tokens** (`ledger.ts:71-74`) |
| output_tokens | integer | NO | 0 | |
| cached_input_tokens | integer | NO | 0 | cache reads |
| reasoning_tokens | integer | NO | 0 | |
| cost_usd | numeric(12,6) | NO | '0' | computed by `pricing.ts` at call time and frozen ("rates change, rows should not", `chat.ts:91`) |
| latency_ms | integer | YES | | |
| error | text | YES | | failures are costed and counted too |
| created_at | timestamptz | NO | now() | |
Indexes: `llm_calls_user_created_at_idx (user_id, created_at DESC NULLS LAST)`,
`llm_calls_run_id_idx (run_id)`, `llm_calls_purpose_model_idx (purpose, model)`.
Live distribution: score/openai/gpt-5.6-luna 1,035 rows; evaluate/openai/gpt-5.6-terra 51
($1.20); chat/openai 118; extract 36; deepseek-flash rows for chat/score/verify.

### 3.6 Usage and system state (`src/lib/db/schema/usage.ts`)

#### `api_usage` (`usage.ts:19-33`; SQL `0002_broken_random.sql:1-9`) - live rows: 1
Third-party request counters by UTC calendar month; JSearch free tier is 200/month
(`usage.ts:10-18`).
| column | type | null | default | notes |
|---|---|---|---|---|
| id | text | NO | | PK `"<provider>:<YYYY-MM>"` - enables single-statement upsert |
| provider | text | NO | | e.g. `jsearch` |
| period | text | NO | | `YYYY-MM` UTC |
| requests | integer | NO | 0 | |
| updated_at | timestamptz | NO | now() | |
Index `api_usage_provider_period_idx (provider, period)`. Live: `jsearch:2026-09` = 25.

#### `system_state` (`usage.ts:42-48`; SQL `0003_settings_and_system_state.sql:1-5`) - live rows: 1
key text PK; value jsonb NN default `'{}'`; updated_at timestamptz NN now(). Only key
`worker` exists: value `WorkerBeat {pid: number, startedAt: ISO string, concurrency: number}`
(`src/lib/system/heartbeat.ts:20-24`), rewritten every 10 s (`worker/main.ts:131`); stale
after 45 s (`WORKER_STALE_MS`, `heartbeat.ts:18`).

### 3.7 Relationship / cascade graph

```
user ─┬─< session (CASCADE)        ─< account (CASCADE)
      ├─< profiles (1:1, CASCADE)  ─< documents (CASCADE) <── resumes.rendered_document_id (SET NULL)
      ├─< resumes (CASCADE) ── job_match_id ──> job_matches (CASCADE)
      ├─< job_matches (CASCADE) ── job_id ──> jobs (CASCADE)  ── company_id ──> companies (SET NULL)
      ├─< runs (CASCADE) ─< agent_events.run_id (CASCADE), approvals.run_id (CASCADE), llm_calls.run_id (SET NULL)
      ├─< agent_events, approvals, conversations ─< messages (CASCADE), messages.user_id (CASCADE)
      ├─< llm_calls.user_id (SET NULL)
      └─< companies.added_by_user_id (SET NULL)
Soft references with NO FK: job_matches.tailored_resume_id -> resumes.id,
runs.parent_run_id -> runs.id, runs.conversation_id -> conversations.id,
agent_events.approval_id -> approvals.id, jobs.canonical_job_id -> jobs.id.
```
Consequence: deleting a conversation leaves runs.conversation_id dangling (follow-up then
returns "skipped" because `getConversation` returns null, `follow-up.ts:76-77`); deleting a
tailored resume row (never done in code) would leave job_matches.tailored_resume_id dangling.

---

## 4. Migrations vs live schema

| File | Content |
|---|---|
| `drizzle/0000_calm_bloodscream.sql` (306 lines) | 17 enums (`:1-17`), 17 tables (`:18-254`), 21 FKs (`:256-276`), 31 indexes (`:277-307`) incl. `account_provider_account_idx` |
| `drizzle/0001_wealthy_human_cannonball.sql` | `DROP INDEX "account_provider_account_idx";` (only statement) |
| `drizzle/0002_broken_random.sql` | `api_usage` table + `api_usage_provider_period_idx` |
| `drizzle/0003_settings_and_system_state.sql` | `system_state` table; `ALTER TABLE profiles ADD COLUMN settings jsonb DEFAULT '{}'::jsonb NOT NULL` |
| `drizzle/meta/_journal.json` | 4 entries, dialect postgresql, version 7, breakpoints true |

Verification performed:
* `pg_dump --schema-only --schema=public` of the live DB: every CREATE TABLE, default,
  nullability, type, FK (incl. ON DELETE), unique constraint, partial predicate and
  expression index matches the migration SQL. Live adds nothing (no triggers
  `pg_trigger`, no functions in `public`, no CHECKs).
* `information_schema.columns` (195 columns) == columns in `0003_snapshot.json`;
  index/unique/FK names (56) identical; snapshot has 0 check constraints.
* Schema TS files were read against the SQL: identical.
Only-in-SQL details an ORM model would miss: the explicit `DESC NULLS LAST` on 5
indexes, the `'{}'::text[]` array defaults, `numeric` without precision on salaries, the
`WHERE ats_slug IS NOT NULL` / `WHERE is_active` predicates, the GIN expression, the
sequence `agent_events_id_seq`, and the fact that `profiles.settings` is the last
physical column. Postgres server: 18.6.

---

## 5. Query modules (`src/lib/db/queries/**`)

Common rules:
* `userId` first argument in every per-user function (DESIGN.md invariant 8;
  `conversations.ts:7-12`, `matches.ts:7-13`, `profile.ts:7-9`, `resumes.ts:16-20`,
  `spend.ts:6-8`). `companies.ts` and `usage.ts` are deliberately global (shared registry
  / provider quotas, `companies.ts:6-13`).
* Ids from URLs/agents are validated with
  `/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i` before hitting a uuid
  column, so "a malformed id is simply not a row rather than a database error"
  (`conversations.ts:83-84`, `matches.ts:586-587`, `resumes.ts:22-23`, `runs/runs.ts:25-26,251-252`,
  `approvals.ts:628-629`). Functions return `null` / `[]` / `false` instead of throwing.
* No transactions anywhere except `saveTailoredResume` (`resumes.ts:166`). Isolation is
  Postgres default READ COMMITTED; races are handled with conditional UPDATEs
  (`WHERE status = ...`) and `ON CONFLICT`.

### 5.1 `src/lib/db/queries/companies.ts` (global, not user-scoped)

| Function | Signature | SQL shape | Notes / consumers |
|---|---|---|---|
| `listCompanies` (`:15-29`) | `(options?: {status?: CompanyStatus; limit?: number})` -> `Company[]` | `SELECT * FROM companies [WHERE status = $1] ORDER BY name ASC LIMIT coalesce(limit, 500)` | `agents/tools/companies.ts`, `worker/handlers/company.ts`, `scripts/seed-companies.ts` |
| `getCompanyBySlug` (`:31-38`) | `(slug)` -> `Company|null` | `SELECT * ... WHERE slug = $1 LIMIT 1` | `app/actions/companies.ts` |
| `getCompanyByBoard` (`:41-53`) | `(sourceType, atsSlug)` -> `Company|null` | `WHERE source_type = $1 AND ats_slug = $2 LIMIT 1` | `companies/discovery.ts` |
| `upsertCompany` (`:61-91`) | `(input: NewCompany)` -> `Company` | Read-then-write, **no ON CONFLICT, no transaction**: find existing by `(source_type, ats_slug)` when `atsSlug` set, else (or if not found) by `slug`. If found: `UPDATE companies SET name=input.name, domain=input.domain ?? existing.domain, careers_urls=input.careersUrls ?? existing.careersUrls, tags=<merged>, status=input.status ?? existing.status, needs_browser=input.needsBrowser ?? existing.needsBrowser, updated_at=now WHERE id=$id RETURNING *`. Else `INSERT ... RETURNING *`. | Tags are **merged, never replaced** (union preserving existing order, then new; only when `input.tags` non-empty) (`:74-78`). `status` passed by callers is always `"active"`, so a re-seed / re-discovery re-activates an `invalid` company (comment `:79`). `careersUrls: []` (seed passes it) **replaces** existing URLs because `[] ?? x` is `[]`. slug, source_type, ats_slug, discovered_via, added_by_user_id are never updated. Idempotent on (source_type, ats_slug) (`:55-60`); concurrent first inserts can still hit `companies_slug_unique` / partial unique index and throw. Consumers: `companies/discovery.ts:377,429`, `scripts/seed-companies.ts:38`. |
| `staleCompanies` (`:94-109`) | `(staleBefore: Date, limit = 200)` | `SELECT * WHERE status='active' AND (last_fetched_at IS NULL OR last_fetched_at < $1) ORDER BY last_fetched_at ASC NULLS FIRST LIMIT $2` | stale cutoff = now - 2 h (`BOARD_STALE_AFTER_MS`, `companies/refresh.ts:24-28`). Consumers: `app/actions/companies.ts`, `runs/search-run.ts`, `worker/handlers/company.ts` |
| `recordFetch` (`:111-127`) | `({companyId, status, jobCount, markInvalid?})` -> void | `UPDATE companies SET last_fetched_at=now, last_fetch_status=$s, last_fetch_job_count=$n, [status='invalid'], updated_at=now WHERE id=$id` | only a 404 (`BoardNotFoundError`) marks invalid (`refresh.ts:77-85`) |
| `setCompanyStatus` (`:129-139`) | `(id, status)` -> `Company|null` | `UPDATE ... SET status, updated_at WHERE id RETURNING *` | `app/actions/companies.ts` (no UUID guard here - caller passes a known id) |
| `countByStatus` (`:141-146`) | `()` -> `{status, count:int}[]` | `SELECT status, count(*)::int FROM companies GROUP BY status` | tools/companies, seed script |
| `knownSlugs` (`:149-156`) | `(slugs: string[])` -> `Set<string>` | `SELECT slug WHERE slug IN (...)`; empty input -> empty set | **no consumers** (dead) |
| `findCompaniesByNames` (`:164-199`) | `(names: string[])` -> `{found: {name, company}[], missing: string[]}` | One query **per name**: `key = lower(name) stripped of [^a-z0-9]`; `domain = lower(trim(name))` minus `^https?://`, `^www.`, first path segment. `SELECT * FROM companies WHERE status='active' AND (regexp_replace(lower(name),'[^a-z0-9]+','','g') = $key OR regexp_replace(slug,'[^a-z0-9]+','','g') = $key OR regexp_replace(lower(coalesce(ats_slug,'')),'[^a-z0-9]+','','g') = $key [OR domain = $domain when domain contains '.']) ORDER BY CASE WHEN source_type IN ('greenhouse','lever','ashby') THEN 0 ELSE 1 END LIMIT 1` | Prefers a board over a careers page (`:190`). Names whose key is empty are **silently dropped** from both lists (`:174`). Note `slug` is not lowercased in SQL (slugs are stored lowercase). Consumer: `runs/search-run.ts` |

### 5.2 `src/lib/db/queries/conversations.ts`

| Function | Signature | SQL shape | Notes / consumers |
|---|---|---|---|
| `listConversations` (`:14-21`) | `(userId, limit = 50)` | `SELECT * FROM conversations WHERE user_id=$1 ORDER BY updated_at DESC LIMIT $2` | `app/api/conversations/route.ts` (limit 30) |
| `getConversation` (`:24-32`) | `(userId, id)` -> row or null | UUID guard; `WHERE id=$id AND user_id=$u LIMIT 1` | null if missing **or** someone else's. chat page, `api/conversations/[id]`, `chat/follow-up.ts` |
| `deleteConversation` (`:35-42`) | `(userId, id)` -> boolean | UUID guard; `DELETE FROM conversations WHERE id AND user_id RETURNING id` | messages cascade; runs.conversation_id left dangling. `api/conversations/[id]` |
| `createConversation` (`:44-50`) | `(userId, title?)` | `INSERT (user_id, title ?? NULL) RETURNING *` | only internal + tests |
| `ensureConversation` (`:64-81`) | `(userId, id: string|undefined, title?)` | If `id` is a UUID: `getConversation`; if absent `INSERT (id, user_id, title) ON CONFLICT DO NOTHING RETURNING *`; if that returned nothing (id held by another user) fall through to `createConversation` (fresh id). | Why (`:52-63`): the client picks the id before sending so it can navigate to `/chat/<id>`; the server **claims** it; an id owned by someone else can never be attached. Consumer `app/api/chat/route.ts:38` |
| `loadMessages` (`:86-107`) | `(userId, conversationId)` -> `UIMessage[]` | `SELECT * FROM messages WHERE conversation_id=$c AND user_id=$u ORDER BY created_at ASC` -> `{id, role, parts, metadata ?? undefined}` | rows inserted in the same statement share `now()` and have no tie-breaker. chat route, chat page, `api/conversations/[id]`, follow-up |
| `saveMessages` (`:116-148`) | `(userId, conversationId, uiMessages[])` | no-op on empty; `INSERT INTO messages (id, conversation_id, user_id, role, parts, metadata) VALUES ... ON CONFLICT (id) DO UPDATE SET parts = excluded.parts, metadata = excluded.metadata`; then `UPDATE conversations SET updated_at=now WHERE id=$c` | Upsert because a multi-step agent can persist the same message id more than once (`:109-115`). The conflict update is **not scoped to user_id/conversation_id** (see hazards). role, created_at, conversation_id never change on update. Consumers: chat route, follow-up |

### 5.3 `src/lib/db/queries/matches.ts`

Shared projection `columns` (`:62-87`) used by `listMatches` / `getMatchRows`, from
`job_matches JOIN jobs ON jobs.id = job_matches.job_id`:
`matchId=jm.id, jobId=jobs.id, stage, stageChangedAt, fastScore, deepScore,
score = coalesce(deep_score, fast_score), recommendation = deep_eval->>'recommendation',
headline = coalesce(deep_eval->>'summary', fast_eval->'fit_reasons'->>0), title,
company=jobs.company_name, locations, remote, salaryMin = salary_min::float8, salaryMax =
salary_max::float8, salaryCurrency, salaryPeriod, postedAt, url, applyUrl, isActive,
tailoredResumeId, userRating, createdAt=jm.created_at` (type `MatchRow`, `:31-58`).
Constants: `OPEN_STAGES` = found, shortlisted, applied, interviewing, offer;
`CLOSED_STAGES` = rejected, archived; `ALL_STAGES` (`:17-25`).

| Function | Signature | SQL shape | Notes / consumers |
|---|---|---|---|
| `listMatches` (`:104-142`) | `(userId, {stages?, minScore?, query?, evaluatedOnly?, jobIds?, limit?, offset?})` -> `MatchRow[]` | `SELECT <columns> FROM job_matches JOIN jobs ... WHERE jm.user_id=$u [AND stage IN (...)] [AND coalesce(deep,fast) >= $min] [AND deep_eval IS NOT NULL] [AND job_id IN (...)] [AND (jobs.title ILIKE %q% OR jobs.company_name ILIKE %q%)] ORDER BY coalesce(deep,fast) DESC NULLS LAST, jobs.posted_at DESC NULLS LAST LIMIT coalesce(limit,50) OFFSET coalesce(offset,0)` | `jobIds: []` returns `[]` without querying (`:119-121`); `query` trimmed, `%`/`_` not escaped. Pagination = limit/offset. Consumers: home (limit 6), chat page (limit 1), matches page (limit 1000), `agents/tools/search.ts` (limit default 10) |
| `getMatchRows` (`:144-156`) | `(userId, matchIds[])` | same projection `WHERE jm.user_id=$u AND jm.id IN (...) ORDER BY score DESC NULLS LAST`; `[]` -> `[]` | **no UUID guard** (callers filter). activity page, actions/matches, api/matches, tools/matches, follow-up, evaluate-run |
| `getMatchDetail` (`:164-178`) | `(userId, matchId)` -> `{match: JobMatch, job: Job}|null` | UUID guard; `SELECT jm.*, jobs.* ... WHERE jm.user_id AND jm.id LIMIT 1` | match page, tailor-run, worker resume handler |
| `getMatchByJob` (`:181-191`) | `(userId, jobId)` | `SELECT * FROM job_matches WHERE user_id AND job_id LIMIT 1` | tools/search |
| `resolveMatch` (`:197-212`) | `(userId, matchOrJobId)` | if UUID: by `id` (scoped); else / if not found: `getMatchByJob` | accepts either id because "asking [the agent] to keep them apart is a failure mode" (`:193-196`) |
| `ensureMatch` (`:218-231`) | `(userId, jobId)` -> `JobMatch` | `getMatchByJob`; else `INSERT (user_id, job_id, stage='found') ON CONFLICT DO NOTHING RETURNING *`; if nothing returned, re-read | race-safe create. tailor-run |
| `resolveOrCreateMatch` (`:238-251`) | `(userId, matchOrJobId)` -> `JobMatch|null` | `resolveMatch`; else `SELECT id FROM jobs WHERE id=$1 LIMIT 1` then `ensureMatch` | tools/matches |
| `countByStage` (`:253-268`) | `(userId)` -> `Record<Stage, number>` | `SELECT stage, count(*)::int ... WHERE user_id GROUP BY stage`; every stage pre-filled with 0 | home, matches page |
| `setMatchStage` (`:274-289`) | `(userId, matchId, stage)` -> row or null | `UPDATE job_matches SET stage=$s, stage_changed_at = CASE WHEN stage = $s THEN stage_changed_at ELSE now() END, updated_at=now WHERE user_id AND id RETURNING *` | same-stage move is a successful no-op that keeps the timestamp (`:270-273`). No UUID guard (callers). actions/matches, tools/matches |
| `updateMatch` (`:291-306`) | `(userId, matchId, {notes?, userRating?})` | `UPDATE SET [notes], [user_rating], updated_at WHERE user_id AND id RETURNING *` | `undefined` = leave alone, `null` = clear. actions/matches |
| `countNewSince` (`:309-324`) | `(userId, since)` -> `{total, strong}` | `count(*)::int, count(*) FILTER (WHERE coalesce(deep,fast) >= 80)::int WHERE user_id AND created_at >= $since AND stage IN OPEN` | **no consumers** |
| `latestMatchedAt` (`:326-334`) | `(userId)` -> Date or null | `SELECT created_at ... ORDER BY created_at DESC LIMIT 1` | **no consumers** |
| `selectForDeepEval` (`:359-391`) | `(userId, {jobIds, minFastScore, limit})` -> `DeepEvalCandidate[]` | `[]` if limit<=0 or no jobIds. `SELECT jm.id matchId, jobs.id jobId, title, company_name, fast_score, coalesce(length(description_text),0)::int descriptionChars FROM job_matches JOIN jobs WHERE jm.user_id AND job_id IN (...) AND stage IN ('found','shortlisted') AND fast_score >= $min AND deep_eval IS NULL AND jobs.is_active = true ORDER BY fast_score DESC, posted_at DESC NULLS LAST LIMIT $limit` | Limited to the run's own postings - it used to top up with older matches (`:349-358`, invariant 16). search-run |
| `matchesNeedingEvaluation` (`:398-419`) | `(userId, matchIds)` -> `string[]` | UUID-filter ids; `SELECT id WHERE user_id AND id IN (...) AND deep_eval IS NULL AND stage IN OPEN`; returns ids in **input order** | evaluate-run |
| `isShortlistCandidate` (`:425-432`) | pure | `stage === 'found' && recommendation in (strong, apply)` | evaluate-run |
| `listShortlistCandidates` (`:435-444`) | `(userId, matchIds)` | `getMatchRows` filtered by the rule | **no consumers** |
| `moveMatchesFromStage` (`:451-473`) | `(userId, matchIds, from, to)` -> moved ids | `UPDATE SET stage=$to, stage_changed_at=now, updated_at=now WHERE user_id AND id IN (...) AND stage=$from RETURNING id` | one statement so a match the user moved meanwhile stays put (`:446-450`). evaluate-run (shortlist) |
| `topMatchIdsForJobs` (`:479-501`) | `(userId, jobIds, limit = 5)` | `SELECT id WHERE user_id AND job_id IN (...) AND stage IN OPEN AND score IS NOT NULL ORDER BY score DESC, updated_at DESC LIMIT $n` | run output `topMatchIds`. evaluate-run |
| `scoreBandsForJobs` (`:504-525`) | `(userId, jobIds)` -> `{strong, good}` | `count FILTER (score >= 80)`, `count FILTER (score >= 65 AND score < 80)` over open matches of those jobs | evaluate-run |
| `saveDeepEval` (`:536-561`) | `(userId, matchId, evaluation: DeepEval)` | `UPDATE job_matches SET deep_score=$score, deep_eval=$json, updated_at=now [, stage = CASE WHEN stage='found' THEN 'archived'::match_stage ELSE stage END, stage_changed_at = CASE WHEN stage='found' THEN now() ELSE stage_changed_at END, notes = CASE WHEN stage='found' THEN concat_ws(E'\n\n', nullif(notes,''), 'Archived: the deep review recommended skipping this one.'::text) ELSE notes END] WHERE user_id AND id RETURNING *` | archive branch only when `recommendation === 'skip'`; only an untouched (`found`) match moves (`:527-535`). `ranking/deep-eval.ts` |
| `appendMatchNote` (`:567-584`) | `(userId, matchId, note)` | trims; empty -> null without query. `UPDATE SET notes = CASE WHEN coalesce(notes,'') = '' THEN $t ELSE notes || E'\n\n' || $t END, updated_at WHERE user_id AND id RETURNING *` | chat must not erase what the user typed (`:563-566`). tools/matches |

### 5.4 `src/lib/db/queries/profile.ts`

| Function | Signature | SQL shape | Notes / consumers |
|---|---|---|---|
| `getProfile` (`:11-18`) | `(userId)` -> `Profile|null` | `SELECT * FROM profiles WHERE user_id LIMIT 1` | settings page, actions/profile, deep-eval, onboarding, search-run, tailor-run |
| `getBaseResume` (`:20-28`) | `(userId)` | `SELECT * FROM resumes WHERE user_id AND kind='base' ORDER BY version DESC LIMIT 1` | no tie-breaker if versions collide. match page, profile page, actions/resumes, tools/resume, search-run, tailor-run |
| `getDocument` (`:30-37`) | `(userId, id)` | `SELECT * FROM documents WHERE id AND user_id LIMIT 1` | no UUID guard here; `app/api/documents/[id]/route.ts` guards before calling |
| `recordDocument` (`:43-62`) | `({userId, kind, filename, mime, sizeBytes, storagePath, sha256, textExtracted?})` -> `Document` | `INSERT ... ON CONFLICT (user_id, sha256) DO UPDATE SET text_extracted = $textExtracted ?? NULL RETURNING *` | re-upload returns the existing row (kind, filename, storage_path, created_at of the **first** upload); `text_extracted` overwritten (NULL when not passed - the PDF-render path). Caller deletes the redundant stored file if `storage_path` differs (`worker/handlers/resume.ts:180-182`). Consumers: `app/api/uploads/route.ts:68`, worker resume handler `:171` |
| `saveBaseResume` (`:72-101`) | `({userId, resume, documentId?})` | `getBaseResume`; `INSERT INTO resumes (user_id, kind='base', version=prev+1 or 1, data) RETURNING *`; `INSERT INTO profiles (user_id, data) ON CONFLICT (user_id) DO UPDATE SET data=$resume, updated_at=now` | Creates the profile row on first upload. `documentId` is accepted but **never stored**. Read-then-insert race on version (no unique). Consumer: `app/api/uploads/route.ts:83` |
| `loadBaseResume` (`:112-122`) | `(userId)` -> `{id, version, resume}|null` | `getBaseResume` + `ResumeSchema.safeParse` (null on parse failure) | "the one set of facts scoring, evaluation, tailoring and the chat agent all read" (`:103-111`). deep-eval, tailor-run, evaluate-run |
| `saveEditedResume` (`:129-133`) | `(userId, resume)` | returns current row unchanged if `sameJson(current.data, resume)`; else `saveBaseResume` | `sameJson` (`:135-148`): canonical JSON - object keys sorted, keys with `undefined` **or `null`** values dropped, arrays in order. Consumer: `app/actions/profile.ts` |

### 5.5 `src/lib/db/queries/resumes.ts`

| Function | Signature | SQL shape | Notes / consumers |
|---|---|---|---|
| `getResume` (`:25-36`) | `(userId, id)` | UUID guard; `SELECT * FROM resumes WHERE id AND user_id LIMIT 1` | `api/resumes/[id]/preview`, tailor-run, worker resume handler |
| `getTailoredForMatch` (`:39-57`) | `(userId, matchId)` | UUID guard; `WHERE user_id AND kind='tailored' AND job_match_id ORDER BY version DESC, created_at DESC LIMIT 1` | **no consumers** |
| `listTailored` (`:74-107`) | `(userId, limit = 20)` -> `TailoredSummary[]` | `SELECT r.id, jm.id matchId, jobs.id jobId, title, company_name, version, rendered_document_id, verification, changes, r.created_at FROM resumes r JOIN job_matches jm ON jm.id = r.job_match_id JOIN jobs ON jobs.id = jm.job_id WHERE r.user_id AND r.kind='tailored' ORDER BY r.created_at DESC LIMIT $n`; maps `passed` (bool or null), `unsupported` = len(verification.unsupported), `changes` = len(changes) | **no consumers** |
| `latestBaseRender` (`:110-133`) | `(userId)` -> `{resume, document}|null` | `SELECT r.*, d.* FROM resumes r JOIN documents d ON d.id = r.rendered_document_id AND d.user_id = $u WHERE r.user_id AND r.kind='base' AND r.rendered_document_id IS NOT NULL ORDER BY r.version DESC LIMIT 1` | profile page |
| `markRendered` (`:136-150`) | `(userId, resumeId, documentId, engine = 'html')` | `UPDATE resumes SET rendered_document_id, render_engine, updated_at WHERE id AND user_id` | worker resume handler |
| `saveTailoredResume` (`:157-200`) | `(userId, {matchId, resume, changes, verification})` -> row | **Transaction**: `SELECT coalesce(max(version),0)::int FROM resumes WHERE user_id AND kind='tailored' AND job_match_id=$m`; `INSERT resumes (user_id, kind='tailored', job_match_id, version=max+1, data, changes, verification) RETURNING *`; `UPDATE job_matches SET tailored_resume_id=$new, updated_at WHERE id=$m AND user_id` | "a match never points at a resume that failed to save" (`:152-156`). tailor-run |

### 5.6 `src/lib/db/queries/spend.ts` (ledger reads)

* `monthStart(at = new Date())` -> `Date.UTC(year, month, 1)` (`:10-12`).
* `spendSince(userId, since)` (`:14-25`): `SELECT coalesce(sum(cost_usd),0)::float8 usd,
  count(*)::int calls, coalesce(sum(input_tokens + cached_input_tokens),0)::float8
  inputTokens, coalesce(sum(output_tokens),0)::float8 outputTokens FROM llm_calls WHERE
  user_id=$u AND created_at >= $since`. Home + settings (since = month start).
* `spendByPurpose(userId, since)` (`:27-38`): `purpose, usd, calls ... GROUP BY purpose
  ORDER BY sum(cost_usd) DESC`.
* `spendByModel(userId, since)` (`:40-54`): `provider, model, usd, calls, count(*) FILTER
  (WHERE error IS NOT NULL)::int failures, coalesce(avg(latency_ms),0)::float8 avgLatencyMs
  GROUP BY provider, model ORDER BY sum(cost_usd) DESC`.
* `spendByDay(userId, since)` (`:57-67`): `to_char(date_trunc('day', created_at AT TIME ZONE
  'UTC'), 'YYYY-MM-DD') day, usd GROUP BY 1 ORDER BY 1` - only days with calls.

### 5.7 `src/lib/db/queries/usage.ts` (provider quotas, global)

* `currentPeriod(at = new Date())` -> `"YYYY-MM"` in UTC (`:7-9`).
* `countRequest(provider, count = 1, at = new Date())` (`:18-38`): `INSERT INTO api_usage
  (id='<provider>:<period>', provider, period, requests=$count) ON CONFLICT (id) DO UPDATE
  SET requests = api_usage.requests + $count, updated_at = now RETURNING requests`. Single
  statement so "two concurrent fetchers cannot both read 199 and both decide they have
  room" (`:11-17`). Counted **before** the response is read (`sources/jsearch.ts:274-276`).
* `requestsThisPeriod(provider, at)` (`:40-56`): `SELECT requests WHERE provider AND
  period LIMIT 1`, 0 if none. Checked against `JSEARCH_MONTHLY_BUDGET` (env, default 180,
  `src/env.ts:73`) at `sources/jsearch.ts:249-252`.

---

## 6. DB access outside the query modules (every site, with SQL shape)

These belong to other areas functionally, but they are part of the data contract.

### 6.1 Runs lifecycle - `src/lib/runs/runs.ts`
* `createRun(input: NewRun)` `:19-22` - `INSERT INTO runs ... RETURNING *`.
* `getRun(userId, id)` `:24-33` - UUID guard; `WHERE id AND user_id`.
* `activeSearch(userId)` `:40-54` - `WHERE user_id AND kind='search' AND status IN
  (queued, running, awaiting_approval) ORDER BY created_at DESC LIMIT 1` (one search at a time).
* `listRuns(userId, limit = 20)` `:56-63` - `ORDER BY created_at DESC LIMIT n`.
* `startRun(id)` `:65-70` - `SET status='running', started_at=now, updated_at`.
* `mergeStats(id, patch)` `:73-81` - `SET stats = stats || $json::jsonb` (shallow merge so
  concurrent steps do not clobber, `:9-17,72`).
* `finishRun(id, outcome)` `:83-100` - unconditional `SET status, output (or NULL), error
  (or NULL), finished_at, updated_at`.
* `requestCancel(userId, id)` `:113-141` - `UPDATE SET status='cancelled', finished_at,
  updated_at WHERE id AND user_id AND status IN ('queued','running','awaiting_approval')
  RETURNING *`; then `expireRunApprovals`; emits `run.cancelled` (warn) only if approvals
  were expired (the paused run has no handler to report it).
* `setRunStatusIf(id, from[], to, stats?)` `:152-173` - `SET status=$to, [started_at =
  coalesce(started_at, now()) when to='running'], [stats = stats || $json], updated_at
  WHERE id AND status IN (from) RETURNING id` -> boolean.
* `finishActiveRun(id, outcome, stats?)` `:180-202` - like finishRun but `WHERE status IN
  ACTIVE` so a late "completed" never overwrites a cancel.
* `isCancelled(id)` `:205-212`.
* `reapInterruptedRuns()` `:225-249` - at worker boot: `UPDATE runs SET status='failed',
  error='Interrupted: the worker stopped while this was running. Start it again to
  retry.', finished_at, updated_at WHERE status='running' RETURNING id, user_id, kind`;
  emits `run.failed` per run. Queued and awaiting_approval runs are left alone (`:214-224`).
  Global (all users) - assumes a single worker process.

### 6.2 Approvals - `src/lib/runs/approvals.ts`
* `requestApproval` `:142-237`: autopilot -> `INSERT approvals (... status='auto_approved',
  decision={approved:true[, itemIds: all]}, decided_via='autopilot', decided_at=now)` (no
  expires_at), merge run stats, emit `approval.auto`, enqueue continuation.
  approve_each -> `INSERT (... status='pending', expires_at = now + 7d)`, then conditional
  `UPDATE runs SET status='awaiting_approval', stats = stats || $json, updated_at WHERE id
  AND user_id AND status IN ACTIVE RETURNING id`; if not paused (cancel won) ->
  `UPDATE approvals SET status='expired' WHERE id AND status='pending'`; else emit
  `approval.requested` with `approval_id`.
* `decideApproval(userId, approvalId, decision, via)` `:255-359`: reads row; rejects
  not_found / expired / already_decided; past expiry -> `expireStaleApprovals`; run not
  ACTIVE -> expire. Then exactly-once write `UPDATE approvals SET status, decision,
  decided_via, decided_at WHERE id AND user_id AND status='pending' AND (expires_at IS NULL
  OR expires_at > now()) RETURNING *`; emit `approval.decided`; enqueue; on enqueue failure
  rolls back with `UPDATE SET status='pending', decision=NULL, decided_via=NULL,
  decided_at=NULL WHERE id AND status=$decided` and emits `approval.reopened` (warn).
* `listPendingApprovals` `:459-482`: expire stale first, then `SELECT approvals.* FROM
  approvals JOIN runs ON runs.id = approvals.run_id WHERE approvals.user_id AND
  status='pending' AND (expires_at IS NULL OR expires_at > now()) AND runs.status IN ACTIVE
  ORDER BY approvals.created_at DESC`.
* `getApproval` `:488-498` (pending past expiry reported as `expired` without writing),
  `listRunApprovals` `:501-516` (`ORDER BY created_at` asc), `getApprovalRow` `:518-528`.
* `expireRunApprovals(runId)` `:537-544`: `UPDATE SET status='expired' WHERE run_id AND
  status='pending' RETURNING id`.
* `expireStaleApprovals(userId)` `:553-583`: `UPDATE SET status='expired' WHERE user_id AND
  status='pending' AND expires_at <= now() RETURNING *`; per row emit `approval.expired`
  and **enqueue the continuation** (expiry is a "no" that must still finish the run).
* `mergeRunStats` `:618-626` duplicate of `mergeStats`.
* Continuation validation `parseContinuation` `:602-608`: queue must be a known queue name,
  payload must be an object.

### 6.3 Events and live feed
* `emit` (`runs/events.ts:28-50`): `INSERT INTO agent_events (...) RETURNING *`; follow-up
  enqueue with `SELECT conversation_id FROM runs WHERE id` (`:57-74`), pg-boss option
  `singletonKey: "follow-up-<eventId>"`; failures logged, never thrown.
* `liveSnapshot(userId, since)` (`runs/live.ts:96-172`), 3 queries + heartbeat in parallel:
  events `WHERE user_id AND id > $since ORDER BY id LIMIT 200` (or, when since <= 0, the
  latest 60 `ORDER BY id DESC LIMIT 60` reversed); runs `WHERE user_id AND (status IN
  ACTIVE OR updated_at > now - 10 min) ORDER BY created_at DESC LIMIT 20` (RECENT_MS
  `:69`); approvals `WHERE user_id AND status='pending' AND (expires_at IS NULL OR
  expires_at > now()) ORDER BY created_at DESC LIMIT 20` (note: does **not** join runs, unlike
  listPendingApprovals). `cursor` = last event id or `since`.
* `GET /api/runs/[id]/events` (`app/api/runs/[id]/events/route.ts:28-33`): `SELECT * FROM
  agent_events WHERE user_id AND run_id ORDER BY id ASC LIMIT 1000`.

### 6.4 Heartbeat and settings
* `beat` (`system/heartbeat.ts:26-34`): `INSERT INTO system_state (key='worker', value,
  updated_at) ON CONFLICT (key) DO UPDATE SET value=$v, updated_at=now()`.
* `workerStatus` (`heartbeat.ts:42-57`): online iff `Date.now() - updated_at < 45_000`.
* `getSettings` / `updateSettings` (`profile/settings.ts:38-66`): `SELECT settings FROM
  profiles WHERE user_id`; read-merge-validate-write `UPDATE profiles SET settings=$json,
  updated_at WHERE user_id RETURNING id` (null when no profile row).
* Preference writes: `app/actions/profile.ts:98-105` and
  `agents/tools/update-profile-preferences.ts:54-98` - `SELECT * FROM profiles WHERE user_id`
  then `UPDATE profiles SET preferences, interview_state, updated_at WHERE user_id`
  (read-modify-write, no lock).

### 6.5 Job mirror writes - `src/lib/sources/ingest.ts` (`ingestJobs`, `:28-164`)
* `BATCH_SIZE = 200` (Postgres parameter limit, `:25-26`). Per batch:
  1. `SELECT id, content_hash FROM jobs WHERE id IN (batch)`.
  2. Identical hash -> `UPDATE jobs SET last_seen_at=now, fetched_at=now WHERE id IN (...)`.
  3. New or changed -> `INSERT INTO jobs (all columns, is_active=true, first_seen_at,
     last_seen_at, fetched_at = now) ON CONFLICT (id) DO UPDATE SET company_id, company_name,
     title, locations, location_norm, remote, employment_type, salary_*, description_raw,
     description_text, url, apply_url, posted_at, raw, content_hash, dedupe_key =
     excluded.*, is_active = true, last_seen_at = now, fetched_at = now` (first_seen_at,
     source, external_id, canonical_job_id untouched).
* `deactivateMissing && company` -> `UPDATE jobs SET is_active=false, fetched_at=now WHERE
  company_id=$c AND is_active=true [AND id NOT IN (seen)] RETURNING id`. Only for full board
  fetches (`companies/refresh.ts:56-62`); careers pages and aggregator never deactivate.
* `now` is one JS `Date` for the whole call.
* **Observed bug**: a deactivated posting that reappears with an unchanged hash goes down
  path 2 and stays `is_active = false` (path 3's "open again" never runs). Live: 126
  inactive jobs were seen in their company's latest fetch.
* Other: `companies/refresh.ts:147-152` `UPDATE companies SET needs_browser, updated_at
  WHERE id` (set and cleared).

### 6.6 Ranking
* Prefilter (`ranking/prefilter.ts:298-414`), the heaviest SQL:
  1. `SELECT jm.job_id, roleKey(jobs) FROM job_matches JOIN jobs WHERE jm.user_id=$u`.
  2. `SELECT job_id FROM job_matches WHERE user_id AND stage IN ('rejected','archived')`.
  3. Subquery `deduped` = `SELECT DISTINCT ON (roleKey) id, posted_at FROM jobs WHERE
     is_active = true AND <company clause OR (posted_at IS NULL OR posted_at >= now - 45 d)>
     AND <location clause> AND <title clause> AND canonical_job_id IS NULL [AND id NOT IN
     dismissed] [AND (roleKey NOT IN (known keys) OR id IN (known ids))] ORDER BY roleKey,
     CASE WHEN source = 'jsearch' THEN 1 ELSE 0 END, posted_at DESC NULLS LAST`; outer
     `SELECT id FROM deduped ORDER BY posted_at DESC NULLS LAST LIMIT coalesce(limit, 600)`.
  4. `SELECT count(*) FILTER (WHERE is_active)::int FROM jobs` (funnel).
  `roleKey` (`:596-598`) = `lower(regexp_replace(company_name, '[^a-zA-Z0-9]+', '', 'g')) ||
  '|' || btrim(lower(regexp_replace(title, '[^a-zA-Z0-9+#]+', ' ', 'g')))`.
  Company clause (`:423-434`): `company_id IN (...)` OR per name `company_name ~* '^w1[^a-z0-9]+w2\y'`
  (`companyNamePattern`, `:441-450`, `\y` = PG word boundary). Location clause
  (`:486-531`): `location_norm->>'metro' IN ('bay-area')`, `locations::text ~* <BAY_AREA_PLACES
  regex :460-484>`, per city `locations::text ILIKE '%city%'`, `remote = 'remote'` when
  remote_pref remote/any, `remote = 'hybrid'` when hybrid; all OR-ed. Title clause
  (`:540-551`): `title ~* include AND (title !~* exclude OR title ~* keep)`. Patterns are
  LLM-produced regex alternations joined with `|`. `MAX_AGE_DAYS = 45` (`:35`).
* Fast score (`ranking/fast-score.ts`): `SELECT job_id FROM job_matches WHERE user_id AND
  job_id IN (...) AND fast_score IS NULL` (`:129-138`, never re-score); per batch of 40
  `SELECT id, title, company_name, locations, remote, salary_min, salary_max,
  description_text FROM jobs WHERE id IN (...)` (`:158-170`); per posting `UPDATE
  job_matches SET fast_score, fast_eval, [stage='archived', stage_changed_at=now,
  notes='Auto-archived: scored N, below the T threshold.'], updated_at WHERE user_id AND
  job_id AND stage='found'` (`:197-219`; note: auto-archive **replaces** notes).
  `scoreSummary(userId)` `:252-264` (count FILTERs: scored, >=80, 65-79, archived).
* Deep eval (`ranking/deep-eval.ts:372-390`): `SELECT jobs.<12 fields> FROM job_matches JOIN
  jobs WHERE jm.user_id AND jm.id LIMIT 1`, then `saveDeepEval`.
* Bake-off fixture (`ranking/bakeoff-fixture.ts:48-64`): `SELECT ... FROM jobs WHERE
  description_text IS NOT NULL AND length(description_text) BETWEEN 1500 AND 12000 ORDER BY
  id LIMIT 20` (script only).

### 6.7 Run pipelines
* `search-run.ts:550-558` `saveInput`: `UPDATE runs SET input = input || $json::jsonb, updated_at`.
* `search-run.ts:636-667` `upsertMatches`: batches of 500; `SELECT job_id FROM job_matches
  WHERE user_id AND job_id IN (...)`, then `INSERT (user_id, job_id, stage='found') ...
  ON CONFLICT DO NOTHING` for the fresh ones (existing matches untouched, `:629-635`).
* `evaluate-run.ts:530-542` dedupe of in-flight rank_deep: `WHERE user_id AND
  kind='rank_deep' AND status IN ('queued','running') AND input -> 'matchIds' = $sorted::jsonb`.
* `evaluate-run.ts:818-839` `resumeFrom(runId, approvalId, stats)`: `UPDATE runs SET
  status='running', stats = stats || {..., resumedFrom: approvalId}, updated_at WHERE id AND
  status IN (queued, running, awaiting_approval) AND (stats ->> 'resumedFrom') IS DISTINCT
  FROM $approvalId RETURNING id` - idempotency against pg-boss double delivery.
* `tailor-run.ts:265-283` `activeTailorRun`: `WHERE user_id AND kind='tailor' AND status IN
  ('queued','running') AND input->>'matchId' = $m AND created_at > now - 15 min ORDER BY
  created_at DESC LIMIT 1` (double-click guard; stuck runs older than 15 min do not block).
* `tailor-run.ts:305-310`: `SELECT id FROM jobs WHERE id = $1 LIMIT 1`.

### 6.8 Chat
* `chat/follow-up.ts`: `SELECT * FROM agent_events WHERE id AND user_id LIMIT 1` (`:68-72`);
  `SELECT name FROM "user" WHERE id` (`:122-126`); on failure `DELETE FROM messages WHERE id =
  $noticeId` (`:167`, an update with no reply must not stay in history); `replyInFlight`
  (`:193-203`): last message `ORDER BY created_at DESC LIMIT 1`, in flight if role='user',
  not a run_update, and younger than 90 s (`REPLY_WAIT_MS`, poll 1.5 s `:60-61`);
  `alreadyFollowedUp` (`:205-218`): `WHERE conversation_id AND metadata->>'kind' =
  'run_update' AND (metadata->>'eventId')::bigint = $eventId LIMIT 1` (unindexed jsonb scan).
* `agents/chat-agent.ts:55-59`, `agents/tools/get-profile-summary.ts:23-27`: `SELECT * FROM
  profiles WHERE user_id LIMIT 1`. `agents/tools/search.ts:280-284`: `SELECT * FROM jobs
  WHERE id LIMIT 1` (global mirror read).

### 6.9 Pages / actions (Next server code that queries directly)
* `app/(app)/home/page.tsx:138-156` `liveCounts`: `count(*)::int FROM approvals WHERE
  user_id AND status='pending' AND (expires_at IS NULL OR expires_at > now())`; `count(*)::int
  FROM runs WHERE user_id AND status IN ('queued','running')`.
* `app/(app)/activity/page.tsx:59-107` `describeRuns`: `SELECT id, kind, job_match_id FROM
  resumes WHERE user_id AND id IN (...)`; `SELECT run_id, coalesce(sum(cost_usd),0)::float8
  FROM llm_calls WHERE user_id AND run_id IN (...) GROUP BY run_id`; then `getMatchRows`.
  `listRuns(user.id, 30)`.
* `app/(app)/companies/page.tsx:33-40`: `SELECT * FROM companies ORDER BY name LIMIT 2000` and
  `SELECT company_id, count(id) FROM jobs WHERE is_active GROUP BY company_id` (two queries
  on purpose: Drizzle rendered a correlated subquery unqualified, `:26-32`).
* `app/actions/companies.ts:180-186`: `count(id) FROM jobs WHERE company_id AND is_active`.
* `app/(app)/matches/[id]/page.tsx:50-58`: tailored resume `WHERE id = match.tailored_resume_id
  AND user_id`.

### 6.10 LLM ledger (`src/lib/llm/ledger.ts:54-86`)
`INSERT INTO llm_calls (user_id, run_id, purpose, provider, model, input_tokens =
noCache + cacheWrite, output_tokens, cached_input_tokens = cacheRead, reasoning_tokens,
cost_usd = costUsd(ref, tokens).toFixed(6), latency_ms, error)`. user/run come from
AsyncLocalStorage (`llm/context.ts`). Write errors are swallowed ("losing a ledger row must
not fail work that already spent the money", `:83-84`). Streams record on the `finish`
part or on flush (`:134-166`); failures record zero tokens with `error` message.

### 6.11 Scripts and tests
* `scripts/seed-companies.ts:29-91`: reads `src/lib/companies/seed.json` (entries `{name, slug,
  sourceType: greenhouse|lever|ashby, atsSlug, domain?, tags?}`), `upsertCompany(...,
  careersUrls: [], status: 'active', discoveredVia: 'seed')`; `--fetch` refreshes all active.
* `scripts/dev-session.ts`, `scripts/set-password.ts` (section 3.1).
* Tests (`src/test/unit/approvals.test.ts`, `follow-up-db.test.ts`, `search-gates.test.ts`)
  run against the dev DB: insert a throwaway `user` (`id`, `name`, `email
  <id>@example.test`), rely on CASCADE in `afterAll` delete, mock `@/lib/queue/send`.
  llm_calls rows from tests survive with `user_id = NULL`.

---

## 7. Raw SQL inventory (Postgres-specific constructs that must be reproduced)

| Construct | Where |
|---|---|
| `coalesce(deep_score, fast_score)` as "score" in SELECT, WHERE, ORDER BY ... DESC NULLS LAST, `count(*) FILTER (WHERE ...)` | `queries/matches.ts:60,114,135,313,494-497,512-513` |
| jsonb path `->>`, `->`, `->'fit_reasons'->>0` | `matches.ts:70-71`, `follow-up.ts:212-213`, `tailor-run.ts:274`, `evaluate-run.ts:538,834`, `prefilter.ts:501` |
| jsonb shallow merge `col || $json::jsonb` | `runs/runs.ts:77,166,194`, `approvals.ts:206,622`, `search-run.ts:554`, `evaluate-run.ts:827` |
| jsonb equality `input -> 'matchIds' = $::jsonb` | `evaluate-run.ts:538` |
| `IS DISTINCT FROM` | `evaluate-run.ts:834` |
| `::float8`, `::int`, `::bigint`, `'archived'::match_stage`, `$::text` | `matches.ts`, `spend.ts`, `follow-up.ts:213`, `matches.ts:544-546` |
| `CASE WHEN stage = $s THEN stage_changed_at ELSE now() END` | `matches.ts:283,544-546` |
| `concat_ws(E'\n\n', nullif(notes, ''), ...)`, `notes || E'\n\n' || $t` | `matches.ts:546,578` |
| `ON CONFLICT ... DO UPDATE SET x = excluded.x` / `requests = requests + $n` / `DO NOTHING` | `conversations.ts:76,135-141`, `profile.ts:56-59,95-98`, `usage.ts:28-34`, `heartbeat.ts:30-33`, `ingest.ts:109-138`, `matches.ts:228`, `search-run.ts:661` |
| `regexp_replace(lower(x), '[^a-z0-9]+', '', 'g')` | `companies.ts:183-185`, `prefilter.ts:597` |
| POSIX regex `~*`, `!~*` with `\y` word boundary; `text[]::text ~* / ILIKE` | `prefilter.ts:431,513,520,544-549` |
| `SELECT DISTINCT ON (expr) ... ORDER BY expr, ...` as subquery | `prefilter.ts:379-397` |
| `NOT IN (param list)` built from an array | `prefilter.ts:366,371` |
| `to_char(date_trunc('day', created_at AT TIME ZONE 'UTC'), 'YYYY-MM-DD')`, `GROUP BY 1 ORDER BY 1` | `spend.ts:60-66` |
| `coalesce(length(description_text), 0)::int`, `length(x) BETWEEN a AND b` | `matches.ts:372`, `bakeoff-fixture.ts:60` |
| `now()` comparisons for expiry | `approvals.ts:306,475,561`, `live.ts:139`, `home/page.tsx:147` |
| `coalesce(started_at, now())` | `runs/runs.ts:163` |
| `case when source in ('greenhouse','lever','ashby') then 0 else 1 end` ordering | `companies.ts:191`, `prefilter.ts:388` |
No `sql.raw`, no `db.execute`, no stored procedures.

---

## 8. pg-boss footprint (`pgboss` schema)

Library `pg-boss` ^12.33.2 (`package.json`), schema version row `pgboss.version.version = 42`.

### 8.1 Configuration (`src/lib/queue/boss.ts`)
`new PgBoss({ connectionString: DATABASE_URL, schema: "pgboss", migrate, supervise,
schedule, useListenNotify })` where all four flags are `role === "worker"` (`boss.ts:12-28`):
the worker owns DDL ("if the app migrated it too, two processes could race the same DDL",
`:19-21`), supervision, cron, and LISTEN/NOTIFY wake-ups ("Search started" followed by
activity within a beat, `:24-26`). The Next app is a send-only instance cached on
`globalThis.__foraySender` (`queue/send.ts:14-30`). Each PgBoss instance opens its own pg
pool (separate from the Drizzle pool).

Queue policies (`boss.ts:41-56`), applied by `ensureQueues` with `createQueue` **and**
`updateQueue` (`{...policy, notify: true}`) on every boot of both processes (`:64-70`)
because createQueue is a no-op for existing queues:

| Queue (`queue/queues.ts:8-35`) | retryLimit | expireInSeconds | Worker localConcurrency (`worker/main.ts:50-65`) | Payload (`queues.ts:39-97`) |
|---|---|---|---|---|
| `ping` | 0 | 60 | 1 | `{userId, note?}` |
| `company.discover` | 1 | 900 | 2 | `{userId, names: string[], runId?}` |
| `company.refresh` | 0 | 3600 | 1 | `{userId?, runId?, all?}` |
| `search` | 0 | 3600 | heavy | `{userId, runId}` |
| `search.evaluate` | 0 | 3600 | heavy | `{userId, runId, approvalId}` |
| `search.shortlist` | 0 | 300 | heavy | `{userId, runId, approvalId}` |
| `match.evaluate` | 0 | 1800 | heavy | `{userId, runId, matchIds}` |
| `resume.tailor` | 0 | 1800 | heavy | `{userId, runId, matchId}` |
| `resume.render` | 1 | 600 | 2 | `{userId, runId, resumeId}` |
| `chat.follow_up` | 0 | 300 | 2 | `{userId, runId, eventId: number}` |
`heavy = max(1, min(WORKER_CONCURRENCY (default 4), 4))` (`worker/main.ts:48`). Why
retryLimit 0 for model-spending queues: handlers record their own failure, and an automatic
re-run would pay twice (`boss.ts:31-35`, invariant 15). Follow-ups are not retried because
the news would be stale (`boss.ts:53-54`).

Live `pgboss.queue` rows confirm these values; every queue: policy `standard`, retry_delay
0, retry_backoff false, retention_seconds 1209600 (14 d), deletion_seconds 604800 (7 d),
no dead letter, partition false, table_name `job_common`; plus pg-boss's internal
`__pgboss__send-it` (retry 2, expire 900) used to materialize cron jobs.

### 8.2 API surface used
* `send(queue, payload, options?)` -> job id (`queue/send.ts:33-40`); the id is **discarded**
  everywhere (runs.pgboss_job_id stays NULL). Options used: only `singletonKey:
  "follow-up-<eventId>"` (`runs/events.ts:69`). On a `standard` queue with no
  `singletonSeconds`, pg-boss 12 has no unique index covering this (live indexes
  `job_common_i1..i11` only enforce singleton_key for short/singleton/stately/exclusive/
  key_strict_fifo policies or with singleton_on), so de-duplication actually relies on
  `alreadyFollowedUp()` in `chat/follow-up.ts:205-218`.
* `work(queue, { batchSize: 1, localConcurrency }, handler(jobs[]))` (`worker/main.ts:73-86`);
  handlers receive an array even with batchSize 1; a throw fails the job (pg-boss marks
  `failed`, retries per policy).
* `schedule("company.refresh", "0 */6 * * *", {}, { key: "company-refresh-cron", missed:
  "once" })` (`worker/main.ts:119-122`): every 6 h UTC; `missed: "once"` = a laptop asleep
  overnight catches up with one run. Live `pgboss.schedule` row: name company.refresh, key
  company-refresh-cron, kind cron, timezone UTC, data `{}`.
* `stop({ graceful: true, close: true, timeout: 30_000 })` on SIGINT/SIGTERM
  (`worker/main.ts:140-153`), then `pool.end()`.
* `boss.on("error", ...)` logging only.

### 8.3 Tables (live, pg-boss-owned; do not hand-edit)
`pgboss.version`, `queue` (policy + counters + `notify`), `job` (LIST-partitioned by `name`;
default partition `job_common`; columns id uuid, name, priority, data jsonb, state
job_state, retry_*, expire_seconds, deletion_seconds, singleton_key/on, group_id/tier,
start_after, created_on, started_on, completed_on, keep_until (now()+14 d), output jsonb,
dead_letter, policy, heartbeat_*, blocked/blocking/pending_dependencies, source_*),
`job_dependency`, `schedule`, `subscription`, `bam` (background async migrations),
`warning`, `queue_stats` (RANGE-partitioned by captured_on, daily partitions
`queue_stats_20260918`, `_20260919`). Functions: create_queue, delete_queue, job_now,
job_table_format, job_table_run, job_table_run_async. Live job rows: company.refresh 22
completed + 2 failed, `__pgboss__send-it` 24 completed; nothing queued/active.

### 8.4 Cross-table coupling with app tables
* `approvals.continuation` = `{queue, payload}` is the exact argument to `send()`; the
  queue string must be one of the names above (`approvals.ts:599-608`).
* No app table stores a pg-boss job id, and no app code reads pgboss tables. Comment
  `boss.ts:9-10` claims "a job and the rows it writes can share a transaction" but no code
  does that (send uses its own connection).

---

## 9. Live data snapshot (2026-10-04)

| table | rows | notes |
|---|---|---|
| user | 1 | |
| session | 16 | 7-day TTL, several from dev-session script |
| account | 1 | credential |
| verification | 0 | |
| companies | 227 | 223 active seed/user + 3 invalid + 1 careers_page |
| jobs | 32,115 | 4,755 inactive; greenhouse 23,280, ashby 8,296, lever 296, jsearch 217, web 26 |
| job_matches | 284 | found 98, shortlisted 10, archived 176 |
| profiles | 1 | settings `{}` |
| documents | 2 | 1 resume_upload, 1 resume_pdf |
| resumes | 2 | 1 base v1, 1 tailored v1 (html) |
| runs | 2 | 1 search, 1 tailor (both completed, no conversation) |
| agent_events | 32 | |
| approvals | 2 | deep_eval_batch + shortlist_proposal, both approved via ui |
| conversations / messages | 0 / 0 | |
| llm_calls | 1,404 | total ~$2.60 |
| api_usage | 1 | jsearch:2026-09 = 25 |
| system_state | 1 | worker heartbeat (fresh) |

---

## 10. Invariants that must survive the port (summary)

1. Every per-user read/write filters by `user_id` passed first; shared tables are only
   `companies`, `jobs`, `api_usage`, `system_state` (DESIGN.md invariant 8).
2. Malformed uuid strings yield "not found", never a DB error (UUID regex guards).
3. `jobs.id` = `source:company_slug:external_id` / `web:<slug>:<sha1(url)>`; stable across
   refetches; `description_raw` never rewritten; `first_seen_at` never updated; vanished
   board postings set `is_active=false`, never deleted; only full board fetches deactivate.
4. Ingest skips unchanged rows by `content_hash` (only bumps last_seen_at/fetched_at).
5. `job_matches` unique per (user, job); search never resets an existing match's stage;
   fast scoring only touches `stage='found'` rows and never re-scores (`fast_score IS NULL`);
   deep "skip" archives only `found` matches and appends a note.
6. Stage changes set `stage_changed_at` only when the stage actually changes.
7. Notes from chat are appended with a blank line, never replace user text.
8. Base resumes are versioned (max+1) and `profiles.data` mirrors the newest; tailored
   versions are per match and saved atomically with `job_matches.tailored_resume_id`.
9. `documents` unique per (user, sha256); re-upload returns the existing row.
10. `runs.stats` / `runs.input` are only ever shallow-merged (`||`), never overwritten;
    status transitions are conditional (`WHERE status IN (...)`) so cancel always wins;
    resume-from-approval is idempotent via `stats.resumedFrom`; booting worker fails
    `running` runs.
11. Approvals: decided exactly once via conditional UPDATE; every decision, rejection and
    expiry enqueues the continuation; pending approvals expire after 7 days; enqueue failure
    reopens the approval.
12. `agent_events.id` is a monotonic bigint cursor (`/api/live?since=`); events with a
    run_id and a FOLLOW_UP kind enqueue a chat follow-up when the run has a conversation.
13. Every LLM call writes an `llm_calls` row (including failures), cost frozen at call time,
    write failures never fail the caller.
14. `api_usage` counter increments in one upsert statement, counted before reading the
    response; period is UTC `YYYY-MM`.
15. Conversation ids are claimed from the client but never attached across users;
    messages upsert by AI SDK id (non-empty).
16. Worker heartbeat every 10 s into `system_state['worker']`; offline after 45 s.
17. Queue policies: retryLimit 0 on model-spending queues; per-queue concurrency; cron
    `0 */6 * * *` UTC with catch-up once.

---

## 11. Porting hazards (Django / Python)

1. **Postgres enum types.** Django has no native PG enum field. Options: keep the DB enums
   and use `managed = False` models with a custom `Field` whose `db_type` returns the enum
   name (values are plain strings over psycopg); or migrate to `text` + `CHECK`/`choices`
   (schema change; enum-order comparisons are not used anywhere in SQL, but `'archived'::match_stage`
   casts are). Adding members later needs `ALTER TYPE ... ADD VALUE` (cannot run inside a
   transaction block before PG12 semantics; fine on PG18 but not usable in the same
   transaction it is added).
2. **`timestamp without time zone` on the 4 Better Auth tables** while everything else is
   `timestamptz`. With `USE_TZ=True` Django will read naive datetimes (and warn on writes).
   Values are UTC wall-clock. Either convert those columns to timestamptz (`ALTER ... TYPE
   timestamptz USING col AT TIME ZONE 'UTC'`) when replacing Better Auth, or wrap fields.
3. **Better Auth tables vs `django.contrib.auth`.** Text ids (32-char), scrypt hashes in
   `account.password` (`salt_hex:key_hex`, 161 chars; Better Auth defaults N=16384, r=16,
   p=1, dkLen=64), signed cookie `better-auth.session_token` = `token.base64(HMAC-SHA256)`.
   A Django port needs a custom password hasher (or forced reset) and either a custom
   `User` model on table `user` with a text PK or an id-mapping migration touching every
   `user_id` FK (13 FKs). `user` is a reserved word (Django quotes it, raw SQL must too).
4. **Unbounded `numeric`** (`jobs.salary_min/max`): Django `DecimalField` requires
   max_digits/decimal_places; use a custom field or `managed=False`. `cost_usd` is
   `numeric(12,6)` -> `DecimalField(12, 6)`; existing queries cast to float8 for JSON.
5. **Index features**: partial unique `companies_source_ats_slug_idx`, partial
   `jobs_active_posted_at_idx`, expression GIN `jobs_fts_idx`, and 5 `DESC NULLS LAST`
   btrees. Django can express them (`UniqueConstraint(condition=...)`, `Index(condition=...)`,
   `GinIndex(SearchVector...)` won't reproduce the exact expression; use `Index(expressions)`
   or `RunSQL`) but autogenerated migrations will diverge in names/expressions. Recommended:
   adopt the existing schema with `managed = False` or a hand-written initial migration
   applied with `--fake-initial`, and keep index names.
6. **Text primary keys** (`jobs.id` up to 466 chars for jsearch, `messages.id`,
   `api_usage.id`, `system_state.key`, auth ids) and a **bigserial** (`agent_events.id` ->
   `BigAutoField`, sequence `agent_events_id_seq` must keep its current value).
7. **`text[]` arrays** -> `ArrayField(TextField())` with `default=list`; prefilter casts
   `locations::text` and regex-matches the array literal text (`{"San Francisco, CA",...}`)
   - must be raw SQL in Django.
8. **Raw SQL-heavy queries** (prefilter `DISTINCT ON` subquery with regex, roleKey
   expression, `count FILTER`, jsonb `||` merges, `IS DISTINCT FROM`, `concat_ws`) are
   easier to port as `connection.cursor()` SQL or `RawSQL`/`Func` expressions than as pure
   ORM. Regex patterns are LLM-generated and fed to `~*`; Python `re` vs PG ARE semantics
   differ (`\y`), keep matching in SQL.
9. **`jsonb ||` shallow merge for concurrency**: Django `.update(stats=F('stats') + ...)`
   does not exist for jsonb; use `RawSQL("stats || %s::jsonb", [json.dumps(patch)])` or
   `Func(..., template="%(expressions)s || %s")`. Never read-modify-write in Python.
10. **ON CONFLICT semantics**: `bulk_create(update_conflicts=True, unique_fields=[...],
    update_fields=[...])` covers most upserts, but not `requests = requests + 1 RETURNING
    requests` (api_usage), not `RETURNING` on DO NOTHING (ensureMatch/ensureConversation
    need to know whether the insert happened), not the `excluded.*` + literal mix in ingest.
    Use raw SQL for those.
11. **content_hash / dedupe_key / webJobId must be byte-identical** to keep existing rows
    "unchanged": `JSON.stringify([title, sortedLocations, descriptionRaw ?? "", salaryMin ??
    null, salaryMax ?? null])` -> Python must use `json.dumps(..., separators=(",", ":"),
    ensure_ascii=False)` and format numbers like JS (e.g. `150000` not `150000.0`, JS
    shortest float repr), and JS `sort()` (UTF-16 code unit order) for locations.
    Mismatch would rewrite all 32k rows on the first refresh (harmless but expensive) and
    break `first_seen_at`-based semantics only if ids change (they do not).
12. **UTC month/day boundaries** (`monthStart`, `currentPeriod`, `spendByDay AT TIME ZONE
    'UTC'`) must stay UTC regardless of Django `TIME_ZONE`.
13. **Dead columns / members** carried by the schema: `jobs.canonical_job_id` (read but
    never written), `runs.parent_run_id`, `runs.pgboss_job_id`, `profiles.assessment`,
    `jobs_fts_idx`, enum members none/paused/company_discovery/company_refresh/
    profile_import/tailor(approval)/company_add/latex; dead query functions `knownSlugs`,
    `createConversation` (internal only), `countNewSince`, `latestMatchedAt`,
    `listShortlistCandidates`, `getTailoredForMatch`, `listTailored`. Port or drop
    deliberately.
14. **Existing bugs / sharp edges to decide on** (reproducing them blindly is a choice):
    * Reappearing unchanged postings stay `is_active=false` (`ingest.ts:58-73`; 126 live rows).
    * `saveMessages` upsert is keyed only on the client-supplied message id and does not
      check `user_id`/`conversation_id` on conflict (`conversations.ts:123-142`): a client
      that reuses another user's message id overwrites its `parts`/`metadata`.
    * `upsertCompany` is read-then-write (unique violation under concurrency) and `[]`
      careers URLs wipe existing ones; seed/discovery re-activate invalid companies.
    * Version numbers (`resumes.version`) have no unique constraint; base saves are not
      transactional.
    * `findCompaniesByNames` drops names with no alphanumerics from both result lists.
    * Fast-score auto-archive overwrites `notes` (deep-eval skip appends).
    * `getMatchRows`/`setMatchStage`/`updateMatch`/`appendMatchNote` have no UUID guard; a
      malformed id raises a PG error unless the caller filtered it.
    * `liveSnapshot` approvals do not join runs, so a pending approval on a non-active run
      (should not exist after expiry logic) would still show.
    * `agent_events` cursor `id > since` can skip an event whose sequence value was
      allocated earlier but committed later than a higher id already read.
15. **Two connection pools per process today** (Drizzle pool 5/10 + pg-boss pool). Django
    with persistent connections (`CONN_MAX_AGE`) + a queue library will have its own
    footprint; Postgres default `max_connections` 100.
16. **pg-boss replacement**: the `pgboss` schema is library-owned (version 42, partitioned
    tables, functions). A Python queue cannot read it; cutover = drain (nothing queued
    live), stop the Node worker, then drop schema `pgboss` or leave it. Approval
    continuations stored in `approvals.continuation` use pg-boss queue names
    (`search.evaluate`, `search.shortlist`, ...); the new task system must accept the same
    `{queue, payload}` documents (or a data migration must rewrite pending ones).
    Semantics to reproduce: per-queue retry limits (0/1), per-queue concurrency, job
    expiry (stuck-active detection: 1-60 min), LISTEN/NOTIFY instant pickup, cron with
    catch-up-once, graceful drain on SIGTERM, at-least-once delivery (handlers are written
    to be idempotent: `resumedFrom`, conditional status updates).
17. **Drizzle migration history**: Django will not read `drizzle.__drizzle_migrations`.
    Start Django migrations from the current schema (`0003` state) with `--fake-initial`
    / `managed=False`, and keep the `drizzle` schema or drop it after cutover.
18. **Tests** use the shared dev database and CASCADE cleanup; a Django test runner would
    by default create a separate test DB - better, but prefilter tests (`search-gates.test.ts:135-139`)
    read real `jobs` rows from the dev mirror.

---

## 12. Python candidates

| Need | Candidate |
|---|---|
| ORM / models | Django 5.2/6.x ORM with `django.contrib.postgres` (`ArrayField`, `GinIndex`, `JSONField`), `managed=False` or hand-written initial migration |
| Driver | `psycopg` 3 (`psycopg[binary,pool]`), Django's built-in psycopg3 pool (`OPTIONS: {"pool": {...}}`) |
| PG enums | `django-pgtrigger` not needed; custom `EnumField` (few lines) or `django-postgres-extra` (has `CITextField`/enum helpers) |
| Raw SQL | `connection.cursor()`, `RawSQL`, `Func`/`ExpressionWrapper`; SQLAlchemy Core only if preferred for the prefilter |
| Job queue replacing pg-boss | **Procrastinate** (Postgres-only, LISTEN/NOTIFY, periodic tasks, queueing locks, retry policies, Django integration, stalled-job recovery); alternatives: `django-tasks` DatabaseBackend (Django 6 tasks API), Chancy, PgQueuer, Dramatiq + `dramatiq-pg`, Celery (needs a broker; contradicts "no Redis") |
| Cron | Procrastinate `@app.periodic(cron="0 */6 * * *")` (has catch-up-once semantics via `periodic_id`/defer); or django-q2 schedules |
| Password hashing compat | custom `BasePasswordHasher` using `hashlib.scrypt(n=16384, r=16, p=1, dklen=64)` reading `salt:key` (verify NFKC normalization against Better Auth source before relying on it) |
| JSON hash compatibility | `json.dumps(separators=(",", ":"), ensure_ascii=False)` + JS-compatible number formatting |
| Bounded concurrency (`pooled`) | `asyncio.Semaphore` + `asyncio.gather` (order-preserving) or `concurrent.futures.ThreadPoolExecutor.map` |
| zod schemas for jsonb | Pydantic v2 models with defaults (mirror `.nullable().default(null)` exactly for preferences/settings leniency) |
