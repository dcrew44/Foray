# Step 5A: deviations and clarifications

Later steps read this before the design. Each entry: what, why, evidence.

## 1. The `score` fake hashes the posting's header, not its job id

- **What.** §8.9 says the `score` fake derives its score "from a stable hash of the job id". The
  fast-score prompt carries no job id (it is pinned byte for byte by the §8.10 goldens), so
  `apps/matching/fakes.py` hashes the prompt's `Title`, `Company` and `Location` lines instead:
  `40 + sha1(...) % 60` for a fast score, `50 + sha1("deep|" + ...) % 50` for a deep review,
  whose recommendation is the rubric's band for that score. Stable per posting, spread across
  the bands, so both gates have something to propose on fakes.
- **Also.** `score` serves two output types: `DerivedSearch` (the brief is empty; the title terms
  are the resolved roles when the chat agent set them, else the saved target roles, lowercased,
  else "software engineer") and `FastScore`. Each override recognises its output schema by its
  properties and returns None otherwise (the 3B contract, `deviations/step-3B.md` §2).
- **Evidence.** `tests/matching/test_fakes.py`: stable and spread scores, bands, citations, and
  a whole search on `LLM_FAKE` (below).

## 2. The search's own stale-set refresh holds `MULTI_BOARD_GATE` (clarification of §10.5)

- **What.** A search that names no companies refreshes every stale board (up to 200) before it
  reads the mirror. It now takes `jobs.refresh.MULTI_BOARD_GATE` for that fan-out and reads the
  stale set once the gate is held. A search limited to named companies refreshes only theirs,
  ungated, as §10.5 says of "the search's targeted refresh".
- **Why.** §10.5's rule is "multi-board refreshes never overlap"; its list of gate holders names
  the cron and stale-set `company_refresh` runs but not the search's own stale-set refresh, which
  is the same fan-out. Reading the set inside the gate means a search that waited behind the
  cron usually finds nothing left to refresh.
- **Evidence.** `test_search_pipeline.py::test_an_unlimited_search_refreshes_the_stale_set_under_the_gate`
  (the gate is held during the refresh) and `::test_named_companies_are_looked_up_refreshed_and_reported`
  (it is not).

## 3. What `output.strong` counts

- **What.** `SearchOutput.strong` is the number of this run's open matches whose score
  (`coalesce(deep_score, fast_score)`) is 80 or more: today's `scoreBandsForJobs` "strong" band,
  now always computed and stored. The `run.completed` sentence is unchanged: it still names the
  fast-score bands only when nothing was deep-evaluated.
- **Why.** §9.4 adds `strong` "so N strong shows" (`runOutcome` reads `output.strong`) without
  defining it; this is the definition today's summary already used.
- **Evidence.** `test_search_pipeline.py::test_approve_each_asks_evaluates_proposes_shortlists_and_completes`
  (`strong: 2`: a deep 92 and a fast 88), and the whole-search tests.

## 4. Progress is written only while the run is running (R4)

- **What.** Every progress event of a pipeline (`run.started`, `search.*`, `source.*`,
  `prefilter.done`, `scoring.*`, `evaluation.*`, `shortlist.*`) goes through `search.note()`:
  one transaction that merges `stats` with `UPDATE runs … WHERE status = 'running' RETURNING`,
  then emits, and does neither when the run is no longer running. The run row's lock orders a
  progress write against a Stop, so no event ever follows the run's terminal event. The
  per-pipeline `run.cancelled` sentences ("Search cancelled…", "Stopped. The N roles already
  evaluated were kept.", "Stopped before anything was evaluated.") are gone: `lifecycle.cancel`
  emits the run's one `run.cancelled` (§9.9).
- **Evidence.** `test_search_pipeline.py::test_a_stop_during_scoring_keeps_the_stop_last`: Stop is
  pressed from inside the fake model mid-scoring; the events end `scoring.start`, `run.cancelled`.

## 5. A budget refusal ends a pass instead of counting as a failed posting

- **What.** `score_matches` and `evaluate_batch` stop scheduling calls after a `BudgetExceeded`
  and raise it once their batch is done, so `run_step` fails the run with the budget's sentence
  (§8.6, acceptance item 15). Any other per-posting error still leaves that posting unscored or
  unevaluated and the pass carries on, as today. A fast score whose match was deleted meanwhile
  counts as failed; `autoArchived` counts the matches actually archived (only `found` ones move).

## 6. API answers the design left open

- `POST /api/searches`: `202 {ok, runId, message}`; the gate is `422`; an active search is
  `409 {ok:false, message, runId}`. The body is `{prompt?, mode?, againFrom?}`; `againFrom` is a
  string and anything but the user's own search is ignored (a malformed id included), as today.
  A run started here stores exactly `{"prompt", "hints"}` in `input`.
- `POST /api/matches/{id}/evaluate`: `202` when a run was queued, `200` with "Already evaluating
  <title> at <company>." when that evaluation is already running (no job is deferred), `422`
  with `NoResumeError`'s sentence, `404` "That match does not exist." for a malformed, unknown or
  another user's id, `500` "Could not start the evaluation just now. Try again in a moment."
  otherwise. The `evaluate:<user>` throttle counts every call that reaches the view.
- `PATCH /api/matches/{id}`: any of `stage`, `notes`, `userRating`; `null` clears notes or the
  rating, a `null` stage is unreadable ("That move could not be read."). A failure on `stage`
  answers "That move could not be read.", any other "That change could not be read.". A stage
  and notes sent together are written in one transaction and answered with the move's sentence.
  `userRating` is a JSON integer from -10 to 10 (strings, booleans and fractions refused, as
  `z.number().int()`); notes are at most 20,000 characters.
- `GET /api/matches?ids=`: entries that are not canonical UUIDs are dropped (not a 400), then
  the first 20 are read; no `ids` answers `{matches: []}`.
- **Observed, not changed:** ninja reads a JSON array body as an object with every field left
  out (`{"body": [...]}` validates against a `Schema` with defaults), so `[]` or `["x"]` posted
  to `/api/searches` starts a default search and to `PATCH /api/matches/{id}` is "Nothing to
  change.". This is ninja's `Schema` behaviour for every endpoint with an all-optional body
  (3A's included), not specific to this step.

## 7. The `match.evaluate` job carries only `run_id`

- **What.** `start_match_evaluation` stores `{matchIds: <sorted>}` in `runs.input` and the task
  reads it from there (TS's payload repeated the ids). The dedupe key is
  `evaluate:<sha1(",".join(sorted match ids))>`, so asking again for the same matches while one
  run is active returns it (`reused=True`).

## 8. Handoffs

- **Step 6 (chat tools).** The services to call, all sync (through `db()`):
  - `start_search_run`: `search.assert_can_search(user)` (raises `PreferencesIncompleteError`
    with `.missing` labels), then `search.start_search(user, prompt=..., hints=clean_brief(...)
    .dump(), mode=..., conversation_id=...)`; `SearchStart.reused` is today's `alreadyRunning`.
  - `evaluate_job`: `queries.resolve_or_create_match`, the `evaluate:<user>` throttle, then
    `evaluate.start_match_evaluation(user, [match_id], conversation_id=...)` (raises
    `NoResumeError` / `UnknownMatchError`); the cost phrase is
    `deep_eval.cost_phrase(deep_eval.estimate_eval_cost(1).per_item_usd)`.
  - `list_jobs` / `get_job` / `move_job_stage`: `queries.list_matches(...)`, `get_match_rows`,
    `resolve_match`, `get_match_by_job`, `set_match_stage`, `append_match_note`; `MatchRow` has
    every field `jobCard` reads.
- **Step 7 (pages).** `queries.count_by_stage`, `count_new_since`, `latest_matched_at`,
  `list_matches` and `MatchRow` (already a camelCase `ApiModel`). A page response that embeds
  `FastScore`/`DeepEval` (the match detail page) puts snake_case components into
  `openapi.json`: `tests/core/test_wire_format.py`'s `SNAKE_CASE_COMPONENTS` then needs entries
  for `FastScore`, `DeepEval`, `MatchedRequirement` and `EvalGap` (§2.1: evaluations keep
  today's spelling). `backend/openapi.json` and `schema.d.ts` were not regenerated here (§5.4).
- **Integration (deferred live checks).** The live 5A check needs `pnpm dev:all`: with
  `LLM_FAKE=1`, then once with real models (< $0.50 by the ledger). Its in-process equivalent on
  fakes is `tests/matching/test_fakes.py::test_a_search_on_fakes_reaches_both_gates_and_shortlists`.

## 9. Checks run in this step

- **pytest** (`FORAY_TEST_DB_NAME=test_foray_app_lane5a`): `tests/matching` 140 passed (five
  repeated runs, no flakes); the full suite 1444 passed, 6 skipped, 10 xfailed (baseline before
  this step: 1303 passed, 7 skipped; R15 is no longer pending). Step 4's `LiveRun.summary` handoff
  now resolves (`test_brief.py::test_the_live_run_card_describes_a_searchs_brief`). `ruff check`, `ruff format --check` (246 files), `pyright` (0
  errors), `tsc --noEmit` and `eslint` are clean.
- **Prompt goldens**, byte for byte: `deriveSearch` (3 cases), fast score (4; the declared
  `Pay: 150000` difference), deep eval (4), plus `summarizeSearchRun` (8) and
  `describeEvaluations` (5) and `describeBrief` (11).
- **Live checks:** deferred to the group integration (they need the dev servers and a worker);
  $0 spent. The legacy `foray` write counters stayed at 41158/1014040/5351.
