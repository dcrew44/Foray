# Foray — design

Foray is an AI-first job search product. You onboard with a resume, answer a short
interview, then say things like *"I want entry-level software engineering jobs in the
Bay Area"* and agents do the work in the background: search many sources, rank every
posting against your profile, tailor your resume per job, and — in later phases — apply
through a browser and email recruiters.

This document records **what is settled and why**. The step-by-step build plan lives in
`~/.claude/plans/i-no-longer-want-luminous-kay.md`; progress is tracked by commits on
`main`.

The previous incarnation of this repo — a Python MCP-server learning prototype — is
preserved under the git tag `python-prototype`.

## Settled decisions

| Topic | Decision |
|---|---|
| Audience | Single user now, **structured for SaaS**: auth, `user_id` on every domain row, user-scoped queries from day one |
| Autonomy | **Per-run toggle**: `approve_each` (default) or `autopilot`; approvals surface in chat and in the activity feed |
| Stack | **TypeScript full-stack**: Next.js 16 app + a separate long-running Node worker; one package, two entry points |
| LLM | **Multi-provider, no default vendor.** AI SDK provider registry (OpenAI, Google, DeepSeek, Anthropic, plus an OpenAI-compatible slot); the model for each *purpose* is an env setting |
| Fetching | **Hybrid: code fetches, agents decide where.** Deterministic adapters (Greenhouse, Lever, Ashby, JSearch) + an agent-managed company registry + an LLM extraction fallback for career pages |
| Browser | Phase 1 uses headless Chromium only to render PDFs. Phase 2 adds a Playwright persistent profile per user behind a `BrowserProvider` interface |
| Outreach | Gmail OAuth send + reply tracking; Apollo/Hunter for recruiter emails (Phase 3) |
| Hosting | **Local first, cloud-ready.** Postgres in Docker Compose; app and worker run natively in dev; production Dockerfiles + a `prod` compose profile exist from Milestone A |
| Resume output | Structured resume → **PDF via HTML template**; a `ResumeRenderer` interface leaves room for a LaTeX (tectonic) renderer to be compared later |
| Budget | ~$100/month of third-party APIs |
| Workflow | Build autonomously per phase, **commit each working step to `main`**, user tries it in the browser and steers |

## Invariants

These are the rules that must survive every later change.

1. **Everything intelligent runs server-side.** The Next.js server handles chat turns;
   the worker runs background pipelines. The browser renders UI and streams events.
   Provider API keys and the job mirror never leave the server.
2. **Job id = `source:company_slug:external_id`.** Career-page postings use
   `web:<company_slug>:<sha1(url)>`. Ids are stable across refetches.
3. **Adapters normalize; models hold data.** No source-specific branching outside
   `src/lib/sources/<source>.ts`.
4. **Store the raw description untouched; clean separately.** `description_raw` is
   never rewritten. `description_text` is the cleaned copy.
5. **Strip boilerplate only by known patterns** (EEO, E-Verify, pay transparency) and
   only in the trailing portion of a posting. Never summarize a description into storage.
6. **`fetched_at` is cache metadata, not a job fact.** Job facts are `posted_at`,
   `first_seen_at`, `last_seen_at`.
7. **Postings that vanish from a source are kept with `is_active = false`.** The mirror
   is history, not a snapshot.
8. **Every per-user query takes `userId` first.** There is no unscoped read path to
   per-user data.
9. **Call sites ask for a purpose, never a model name** (`model('score')`). Vendor
   quirks live in `src/lib/llm/models.ts` only.
10. **Every LLM call writes an `llm_calls` ledger row** (provider, model, purpose,
    tokens, cached tokens, USD, run id). This is enforced by middleware on the
    model itself (`src/lib/llm/ledger.ts`), not by discipline at call sites: an
    agent loop calls the model from inside the SDK, so a wrapper only our own
    helpers used recorded nothing for an entire conversation. Attribution to a
    user or run travels through async-local storage (`llm/context.ts`).
11. **Import boundaries** (enforced by ESLint): Next never imports Playwright; the
    worker never imports `src/app`; both import `src/lib`.
12. **Nothing heavy runs inside a Next request.** Long work is a pg-boss job; chat turns
    are capped with `isStepCount(8)` and a timeout.
13. **One set of resume facts.** The latest `resumes(base)` row is what scoring, deep
    review, tailoring and the chat agent read. Editor saves create a new base version;
    `profiles.data` only mirrors the latest one. (Before this rule, the editor wrote
    `profiles.data` while every model read the base resume, so corrections were
    silently ignored.)
14. **One role, one match.** The prefilter collapses postings to one per role (company +
    normalized title, preferring the company's own board and the newest copy) and skips
    roles the user already has a match for. The ingest dedupe key keeps locations apart
    because the mirror should; a person does not want the same role four times.
15. **No silent re-spend, no zombie runs.** Queues that call models have `retryLimit: 0`
    (their handlers record failure on the run instead), and a booting worker marks any
    run it finds still `running` as interrupted. A user can always re-run a search, and
    because scoring and deep review skip finished work, a re-run resumes rather than
    repaying.
16. **A search follows its request.** Each run reads what was asked into a brief
    (`lib/ranking/brief.ts`): companies, roles, level, places, remote, focus. The brief
    overrides the saved preferences for that run only, and filtering, scoring, deep
    review and the results all read the effective preferences. A search's deep-review
    batch and its results come only from its own postings. (Before this, the request
    was stored "for the record": every search ran on the saved preferences, returned
    the same roles, and topped up its review with leftovers from earlier searches.)
17. **Work started in a chat reports back in that chat.** When a run with a
    `conversation_id` pauses for a decision, finishes or fails, the worker adds an
    automatic update to that conversation and has the chat agent answer it
    (`lib/chat/follow-up.ts`). The follow-up turn has read-only tools, waits for any reply
    the user is still receiving, and skips news that is already stale (a decision
    answered from the card, a cancelled run). (Before this, the agent went quiet after
    starting a run, and the only sign of its result was the card changing.)

## Architecture

```
Browser ─▶ Next.js 16 (pages, /api/chat streaming, /api/live polling, uploads, server actions)
              │ enqueue (send-only pg-boss)              ▲ reads runs / events / approvals / matches
              ▼                                          │
          Postgres 18 (app tables + pgboss schema) ◀─────┤
              ▲ work()                                   │
          Worker (tsx): pg-boss handlers, source fetchers + discovery, LLM pipelines,
                        headless Chromium PDF renderer
```

- **Queue**: pg-boss 12 (Postgres only, no Redis), with per-queue policies in
  `lib/queue/boss.ts` (retry limits, expiry) and LISTEN/NOTIFY wake-ups so a job starts
  the moment it is sent. Each queue has its own concurrency, so a long search never
  blocks a tailoring job the user is waiting on.
- **Progress, one channel**: an append-only `agent_events` table. The app shell's
  `LiveProvider` polls a single endpoint, `/api/live?since=<id>`, which returns new
  events, active and recently finished runs, pending approvals and the worker's
  heartbeat in one read. Every run card, approval card, sidebar badge and toast reads
  from that one snapshot, so they always agree. Cadence adapts: 1.5 s while work is in
  flight, 6 s idle, 30 s in a hidden tab. (Previously each component polled its own
  endpoint.)
- **Worker liveness**: the worker writes a heartbeat to `system_state` every 10 s; the
  sidebar says "Worker offline" when it goes quiet, because a queued search with no
  worker is otherwise indistinguishable from a slow one.
- **Agents**: an AI SDK `ToolLoopAgent` drives chat. Search / rank / tailor / discovery
  are plain pg-boss pipelines with approval gates, not agent loops. Browser-driving agent
  loops arrive in Phase 2.
- **Follow-ups**: `emit()` queues a `chat.follow_up` job for the moments a run reports
  (`approval.requested`, `run.completed`, `run.failed`, `tailor.done`, `tailor.failed`)
  when the run came from a chat. The job stores an automatic update (a user-role message
  marked `run_update`, drawn as a divider) and the agent's reply, then emits
  `chat.followup` so an open chat merges it in; the app toasts it anywhere else.
- **Approval gate pattern** (the durable-workflow substitute reused by every later
  phase): a handler that reaches a gate writes an `approvals` row containing a
  `continuation` (queue + payload), emits `approval.requested`, sets the run to
  `awaiting_approval`, and returns. Deciding — from the UI or a chat tool — enqueues the
  continuation. In `autopilot` the same path auto-approves with `decided_via: 'autopilot'`.

### Data model, in two layers

- **Shared mirror** (`companies`, `jobs`): one continuously refreshed copy of every
  posting pulled from any source, shared by all users. One fetch serves everyone.
- **Per-user layer** (everything else): `profiles` (including `settings`: default
  autonomy, score threshold, deep-review count), `documents`, `resumes`, `job_matches`,
  `runs`, `agent_events`, `approvals`, `conversations`, `messages`, `llm_calls` — each
  carries `user_id`.
- **Process state**: `system_state` (the worker heartbeat) and `api_usage` (third-party
  request quotas by month).

### The search pipeline

A search is one run that moves through phases, pausing at two gates in `approve_each`:

1. read the request into a brief and title terms (cheap model, cached on the run);
   fields the chat agent already resolved win over what is read from the text
2. resolve named companies, registering a board the first time one is mentioned;
   refresh their boards (or every stale board for a broad search), query the
   aggregator unless every named company has a board (3 requests per run)
3. SQL prefilter over the mirror, limited to the named companies, one posting per role
4. fast score every new candidate (cheap model, triage), archive below the threshold
5. **gate: deep review** — the top N unreviewed matches among this run's postings, with
   a cost estimate from `pricing.ts`; autopilot approves it automatically
6. deep review (strong model): requirement-by-requirement evidence quoted from the
   resume, gaps rated learnable or disqualifying, fit on level, location and pay
7. **gate: shortlist** — matches the review recommends; approving moves them to
   Shortlisted
8. complete, with the best matches linked from the run card

Tailoring is separate and on demand: the strong model reorders, selects and rephrases
only what the base resume says; a verifier (deterministic rules plus a cheap-model
check) flags any claim it cannot trace to a base fact, unsupported lines are reverted
once automatically, and the result renders to PDF in the worker.

## Interfaces with one implementation in Phase 1

Defined now so later phases are additive, not a rewrite.

| Interface | Phase 1 | Later |
|---|---|---|
| `JobSourceAdapter` | `greenhouse`, `lever`, `ashby`, `jsearch`, `careers-page` | more boards |
| `ResumeRenderer` | `html` (headless Chromium) | `latex` via tectonic, compared side by side |
| `Storage` | local filesystem | S3 / Blob |
| `BrowserProvider` | — (defined in Phase 2) | local Playwright profile, then Browserbase |

## LLM layer

`createProviderRegistry({ openai, google, deepseek, anthropic, compat })`. There is **no
vendor default in code**: `LLM_DEFAULT=<provider>:<model>` is required, and `LLM_CHAT`,
`LLM_EXTRACT`, `LLM_SCORE`, `LLM_EVALUATE`, `LLM_TAILOR`, `LLM_VERIFY`, `LLM_MERGE`
override it per purpose (`extract` and `merge` need a vision-capable model for the
PDF-image fallback).

Cost is a first-class concern: `pricing.ts` holds per-`provider:model` rates (including
DeepSeek's off-peak bands), `call.ts` writes the `llm_calls` ledger, and
`scripts/model-bakeoff.ts` runs fixed fixtures across a list of models to print cost,
latency, schema-failure rate, and agreement with a reference model. Which models to use
is an open question to be settled with those measurements, not guessed.

## Phase plan

- **Phase 1 — find, rank, tailor** (no submitting). **Complete**: onboarding (resume
  extraction, preferences interview or form), search across ~200 boards + JSearch,
  fast scoring, deep review and shortlisting with approval gates, the pipeline board,
  tailored resumes with a fact-checker, PDF rendering, usage and model settings.
- **Phase 2 — browser agent + auto-apply.** `BrowserProvider`, `applications` and
  reusable `application_answers`, `apply` and `career_page_scan` run kinds, an
  `apply_submit` approval showing the filled form.
- **Phase 3 — outreach.** Gmail via `linkSocial`, `contacts`, `outreach_messages`,
  `send_email` approvals.
- **Phase 4 — SaaS hardening.** Cloud deploy of the same containers, Browserbase,
  S3 storage, usage billing off `llm_calls`, own company dataset, retention policy.

### Deferred: LinkedIn PDF import

Skipped by the user on 2026-09-18, after Step 8. It was to be the first consumer
of the provenance work: the same upload path with `kind: linkedin_pdf`, a
`resume/merge.ts` using the `merge` purpose, and a diff the user accepts, never
overwriting `source: user` items.

Nothing was removed for it — `linkedin_pdf` remains in the `document_kind` enum
and in `ItemSource`, and `/api/uploads` already accepts, stores and parses that
kind; it simply does not merge into the base resume. So this stays a small,
additive piece of work rather than a rewrite.

## Interface

The UI was rebuilt as its own piece of work at the end of Phase 1.

- **Shell**: a quiet sidebar on a lichen-grey canvas, and every page on a raised white
  sheet. Navigation: Home, Matches, Activity; Profile, Companies, Settings; recent chats.
- **Palette**: deep pine ink and primary, one signal colour (marigold) reserved for
  "agents working now" and "decisions waiting on you", brick only for errors. Match fit
  is one sequential pine scale (`--fit-0..5`), never traffic lights: the deeper the
  green, the better the match, so a board reads as a heat map.
- **Type**: Bricolage Grotesque for display (titles, headline sentences, scores),
  Instrument Sans for everything read or operated.
- **Dark by default.** The server renders dark; a script in `<head>` switches to light
  before first paint only for someone who chose Light (or Match system on a light OS).
  The choice lives in the browser (`src/lib/theme.ts`) and is set from Settings →
  Appearance or the account menu.
- **Chat renders tool calls as what they did**: a started run is a live run card (with
  its approval inline when it pauses), a job list is clickable match lines, a saved
  preference is one sentence. Raw JSON is one click away, never the default.
- **Home is a status report**, not a greeting: the headline says what needs attention.

Roadmap items deliberately out of Phase 1: LinkedIn automation through the user's own
account, and a career-counselor onboarding mode (Phase 1 only reserves
`profiles.assessment` and versions the interview config).

## Known risks

| Risk | Mitigation |
|---|---|
| PDF parsing quality | text first, vision fallback on garble, user corrects in the editor, fixtures across layouts |
| LLM extraction errors on career pages | used only where no API exists; posting URLs are always real links; unknown fields stay `unknown`; `needs_browser` flag for JS-rendered sites |
| Discovery false positives | a company is registered only after a live API call returns postings; ambiguous names become a `company_add` approval |
| Cost per run | per-purpose model choice, prompt caching, skip-already-scored, score threshold, `deep_eval_top_n`, off-peak scheduling — all measured by the ledger |
| Model quality drift across vendors | vendor-neutral prompts with explicit schemas; the bake-off's schema-failure and agreement metrics |
| Rate limits | per-provider token bucket, `p-limit` per source, JSearch request cap per run + monthly counter |
| Cross-source duplicates | normalized `dedupe_key`, apply-link → ATS slug matching, `canonical_job_id` |
| API drift vs. model training data | `AGENTS.md` + the `vercel/ai` skill installed; `ai`/`@ai-sdk/*` kept on one minor; versions pinned |
| Tailoring fabrication | fact-grounded prompt + verifier with evidence ids + user approval in `approve_each` |
| Container/native drift | same env schema and migrations in both; the containerized end-to-end checklist is the guard |
