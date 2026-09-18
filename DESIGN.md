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

## Architecture

```
Browser ─▶ Next.js 16 (pages, /api/chat streaming, /api/events polling, uploads, server actions)
              │ enqueue (send-only pg-boss)              ▲ reads runs / events / approvals / matches
              ▼                                          │
          Postgres 18 (app tables + pgboss schema) ◀─────┤
              ▲ work()                                   │
          Worker (tsx): pg-boss handlers, source fetchers + discovery, LLM pipelines,
                        headless Chromium PDF renderer
```

- **Queue**: pg-boss 12 (Postgres only, no Redis).
- **Progress**: an append-only `agent_events` table polled by `/api/events?since=<id>`
  every 2 s while a run is active. SSE via `LISTEN/NOTIFY` is an optional later step.
- **Agents**: an AI SDK `ToolLoopAgent` drives chat. Search / rank / tailor / discovery
  are plain pg-boss pipelines with approval gates, not agent loops. Browser-driving agent
  loops arrive in Phase 2.
- **Approval gate pattern** (the durable-workflow substitute reused by every later
  phase): a handler that reaches a gate writes an `approvals` row containing a
  `continuation` (queue + payload), emits `approval.requested`, sets the run to
  `awaiting_approval`, and returns. Deciding — from the UI or a chat tool — enqueues the
  continuation. In `autopilot` the same path auto-approves with `decided_via: 'autopilot'`.

### Data model, in two layers

- **Shared mirror** (`companies`, `jobs`): one continuously refreshed copy of every
  posting pulled from any source, shared by all users. One fetch serves everyone.
- **Per-user layer** (everything else): `profiles`, `documents`, `resumes`,
  `job_matches`, `runs`, `agent_events`, `approvals`, `conversations`, `messages`,
  `llm_calls` — each carries `user_id`.

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

- **Phase 1 — find, rank, tailor** (no submitting). Milestones: A foundation,
  B onboarding, C search and rank, D tailor, E polish and containers.
- **Phase 2 — browser agent + auto-apply.** `BrowserProvider`, `applications` and
  reusable `application_answers`, `apply` and `career_page_scan` run kinds, an
  `apply_submit` approval showing the filled form.
- **Phase 3 — outreach.** Gmail via `linkSocial`, `contacts`, `outreach_messages`,
  `send_email` approvals.
- **Phase 4 — SaaS hardening.** Cloud deploy of the same containers, Browserbase,
  S3 storage, usage billing off `llm_calls`, own company dataset, retention policy.

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
