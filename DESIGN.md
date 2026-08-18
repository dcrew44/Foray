# JobSearchAgent Design

A learning project: an agentic job-search system built as an MCP server plus skills.

**Architecture in one line:** the MCP server provides *capabilities* (fetch, cache, query job postings — deterministic, no judgment), skills provide *judgment* (evaluation rubrics the model applies), and the agent harness (Claude Code for now) provides the *loop*.

## Data Model

### JobPosting

Sources return different shapes, so the server defines one canonical `JobPosting` (Pydantic model, implemented in `jobposting.py`):

| Field         | Type                          | Notes                                      |
| ------------- | ----------------------------- | ------------------------------------------ |
| `id`          | `str`                         | Globally unique, format `source:company:posting_id`, e.g. `greenhouse:anthropic:12345` |
| `title`       | `str`                         |                                            |
| `company`     | `str`                         | Display casing preserved (lowercase only in `id`) |
| `location`    | `list[str]`                   |                                            |
| `salary`      | `SalaryRange \| None`         | `(min, max)` tuple; see decision below     |
| `posted_date` | `datetime \| None`            | Availability varies by source              |
| `description` | `str`                         | Cleaned by the adapter, not the model      |
| `source`      | `str`                         | e.g. `greenhouse`                          |
| `url`         | `str`                         | Application/posting URL                    |

**Decisions made:**

- **Models hold data, adapters transform data.** Description unescaping/compaction happens in adapter helper functions, never in the model's `__init__`. The model must reflect exactly what's stored.
- **Salary stays a field, usually `None`.** Sources rarely provide it structurally, but pay-transparency ranges often appear in description text — a deterministic regex extraction in the adapter can fill it later.
- **`time_fetched` lives in the database only.** It's cache metadata (our relationship to the source), not a fact about the job. Test: the same posting fetched twice is the same `JobPosting`.
- **`Description` should not be cleaned** When stored in the database, it should be stored in full. The stripping and compaction can be done later.

**Active Issues:**
- **May want to replace Salary with less specic Compensation** Currently salary won't cover monthly or hourly paid roles.
## Sources & Adapters

Initial sources: **Greenhouse, Lever, Ashby** — all offer free, no-auth, per-company job-board APIs.

**Key constraint:** these are per-company boards, not search engines (e.g. `boards-api.greenhouse.io/v1/boards/{company_token}/jobs`). There is no global search endpoint, so the server maintains a **company list** and `search_jobs` filters locally over fetched postings.

- Company list starts as manual config; `add_company` / `remove_company` tools later.
- Possible future: use aggregator sites that index Greenhouse companies to discover companies by sector.

**Caching architecture** (also solves slow fetches across N boards):

```
source APIs → adapter (normalize to JobPosting) → SQLite cache → search_jobs queries the cache
```

A refresh step repopulates the cache; `time_fetched` per company board determines staleness.

## Tool Surface

Guiding principle: tools preserve the model's context. Search returns compact summaries; full detail only on drill-down.

### search_jobs(query, location, limit)

Queries the SQLite cache; returns compact summaries only.

- `query: str` — filters on job title (v1); description search later (SQLite FTS5 is the v2 candidate)
- `location: str | list[str] | None = None`
- `limit: int`
- `date_filter` — TBD
- **Returns:** `list[TruncatedJobPosting]` — must include `id` so the model can drill down

### get_job_details(id)

- `id: str` — format `greenhouse:anthropic:12345`; prefix routes to the right adapter
- Strips boilerplate (e.g. EEO text) conservatively — match known patterns, never summarize
- **Returns:** `JobPosting`

### list_sources()

- **Returns:** `list[str]` — configured boards, `source:company` format

### update_job_status(id, status)

- `status`: enum, values TBD — candidate lifecycle: `seen → evaluated → applied → rejected/offer`
- The enum names appear in the tool description, so they're documentation the model reads

## State & Storage

**Decision: hybrid, each store for what it's good at.**

- **SQLite** — structured, queryable state: the posting cache, seen/evaluated/applied status, company list, `time_fetched`. Strong shape guarantees, fast, powers `search_jobs` filtering.
- **Agent-managed files** — qualitative artifacts: evaluation write-ups, tailored resume drafts, notes on why a job was passed on. Lets the agent pull context like "similar jobs I applied to" for resume personalization.
- **Resume** — exposed as an MCP *resource* (not a tool).

### SQL Schema

TBD — design against real adapter output. Guidelines settled so far:

- Columns only for fields that are filtered/sorted on (title, company, location, posted_date, status); full normalized posting in a JSON column beside them
- `companies` table carries per-board `time_fetched` for staleness
- Unique constraint on posting `id`

## Skills

Skills hold the judgment; the server never evaluates.

### compare_job_to_resume

Rubric for evaluating a posting against the resume: skills match, posting age weighting, classifying gaps as learnable vs. disqualifying. (To be written.)

## Future Ideas

- **Red teaming:** a skill where the customized resume/cover letter is evaluated from the perspective of the recruiter who posted the job — fit against the JD, company culture, etc. Run as a subagent.
- Salary regex extraction in adapters (see Data Model).
- FTS5 full-text search over descriptions.
- Company discovery by sector via aggregator indexes.
- Human-in-the-loop application drafting (agent drafts, user reviews and submits).
- Small models can analyze and tag job descriptions in the database.

## Roadmap

1. ~~Canonical `JobPosting` model~~ ✅
2. ~~Greenhouse adapter proven against real data (test script)~~ ✅
3. SQLite layer: schema + cache population ← **next**
4. MCP server wiring (FastMCP): tools over the cache, wired into Claude Code
5. Additional adapters (Lever, Ashby), skills, status workflow
6. Own agent loop (Agent SDK), application automation experiments

# NOTES TO SELF ON NON SELF INFLUENCES

- Did not forsee caching issue myself, was a suggestion from claude, important lesson
- This document reorganization was done by claude (content mine/discussed, structure claude's)
