# Step 7: deviations and clarifications

Later steps read this before the design. Each entry: what, why, evidence.

Step 7 owns `backend/apps/pages/**` (`api.py`, `schemas.py`, `build.py`), `backend/tests/pages/**`
(`conftest.py`, `test_pages.py`, `test_tenancy.py`, `snapshots/*.json`), `backend/openapi.json` and
`src/lib/api/schema.d.ts`. Two small edits outside those files were needed for this step's Done
checks; they are entries 2 and 3.

## 1. Query budgets count the endpoint's own queries (the two auth reads are excluded)

- **What.** `tests/pages/test_pages.py` asserts `django_assert_max_num_queries(budget + 2)` around
  each page request, where the budget is §5.3's and the 2 are the reads every signed-in request
  makes before any endpoint runs: the `django_session` row and the `users` row.
  `test_signed_in_request_costs_two_queries` pins those two exactly (`GET /api/me`, which reads
  nothing more, costs 2, and they are the session and user SELECTs).
- **Why.** §5.3 does not say whether the budgets include them. They cannot: the chat page's
  budget is 3, and with the onboarding state (the step's instruction: from
  `accounts.profile.onboarding_state`, one read of the latest base resume) plus the top match its
  own reads are already 2, so 2 + 2 = 4. Every other page also fits the stricter reading, as the
  table shows. With `SESSION_ENGINE = db` nothing avoids the two auth reads.
- **Evidence** (full user graph, `X-Foray-SSR: 1`; own / whole request / §5.3 budget): home 6 / 8
  / 10, matches 3 / 5 / 5, match 3 / 5 / 5, activity 4 / 6 / 6, chat 2 / 4 / 3, companies 1 / 3 /
  3, profile 3 / 5 / 5, settings 4 / 6 / 6. Mutation: building company rows with `row_for` per
  company (an N+1) fails `test_page_shape_and_query_budget[companies]`.
  `test_activity_page_reads_in_batches` shows 30 runs pointing at resumes and matches stay within
  the activity budget.

## 2. Edits to step 1's `tests/core/test_wire_format.py`

- **What.**
  - `SNAKE_CASE_COMPONENTS` gains `FastScore`, `DeepEval`, `MatchedRequirement` and `EvalGap`
    (the step's instruction; step-5A.md §8): the match page embeds the stored evaluations.
  - New `SNAKE_CASE_VALUES = {"preferences": ...}`: `_camel_keys` still checks the key
    `preferences` but does not descend into its value. Home's `onboarding.preferences` and the
    profile page's `preferences` are the stored `Preferences` (§2.1, §5.3: "byte-compatible"), and
    `test_every_parameterless_get_answers_camel_case` walks every GET without a path id, so it now
    reaches them.
  - `test_chat_stub_answers_503` is replaced by `test_chat_view_refuses_in_plain_text` (`{}` →
    400 `text/plain` "That request could not be read."), step 6's handoff (step-6.md §4).
- **Why.** This step's Done check is that the file "passes over real operations now that none is
  a stub"; without these three changes it failed 3 tests.
- **Evidence.** Before: `test_camel_case_response_and_schema` (DeepEval's `matched_requirements`),
  `test_every_parameterless_get_answers_camel_case` (`/pages/home.onboarding.preferences
  .target_roles`), `test_chat_stub_answers_503` (400 ≠ 503). After: the file passes 10/10.

## 3. Edit to step 5B's `apps/resumes/api.py`: one `200` response, not two

- **What.** `_file_response` keys its response by the int `200` (it used the string `"200"`).
- **Why.** ninja keys an operation's responses by int and deep-merges `openapi_extra` into them, so
  the string key sat beside ninja's own `200` and `export_openapi_schema` wrote the key twice for
  `GET /api/documents/{id}` and `GET /api/resumes/{id}/preview`. `openapi-typescript` refuses a
  duplicated key, so `pnpm api:types` (and with it `pnpm check`'s type-drift step, this step's
  Done check) could not run at all. With the int key the merge gives one `200` with the file's
  content type and description.
- **Evidence.** Before: `pnpm api:types` → `YamlParseError: duplicated mapping key in
  ".../backend/openapi.json" (868:11)`; a JSON parse with `object_pairs_hook` found exactly two
  duplicates, both `"200"`. After: none, and the drift step is clean. The new
  `tests/pages/test_pages.py::test_the_exported_schema_has_no_duplicate_keys` fails with the
  string key put back (mutation checked). `tests/resumes` still passes.

## 4. Shapes the design left open

- **Home.** `user` is `{name}`; `spend` `{usd, calls}` (this UTC month); `waiting` `{approvals,
  running}` (pending unexpired approvals; queued or running runs, as `liveCounts` counted them);
  `counts` is `StageCounts` (all seven stages, zeros included; also the matches page's);
  `headline` is `{headline, detail}` (`describeState`'s return, verbatim); `suggestions` is the
  ask box's strings.
- **Match page.** `match` is `{id, stage, deepEval, fastEval, tailoredResumeId, notes, createdAt,
  stageChangedAt, fastScore, deepScore}` and `job` `{title, companyName, locations, remote,
  salaryMin, salaryMax, salaryCurrency, salaryPeriod, postedAt, isActive, url, applyUrl,
  descriptionText, source}` (the fields the page reads; salaries are numbers, §5.2; never `raw`
  or `descriptionRaw`). `fastEval`/`deepEval` are the stored `FastScore`/`DeepEval`, read
  leniently: an unreadable one is `null`. `tailored` is `TailoredView {id, documentId, createdAt,
  changes, verification}` with `changes` and `verification` passed through as stored (plain
  objects in OpenAPI, as the TypeScript page cast them); typing them would add three more
  snake_case components (`TailorChange`, `UnsupportedClaim`, `Revision`).
- **Activity.** `RunSummary {run: LiveRun, target, costUsd, conversationId}`; `costUsd` is always a
  number (0 without ledger rows). A search's target is its live run's `summary` (the same
  `describeBrief(parseBrief(brief ?? hints))`), else its prompt. No run kind the TypeScript
  created was `company_discovery` or `company_refresh`, so there is no legacy `input.all` case.
- **Chat.** `ChatSuggestion {title, prompt, prefill}`; `prefill` is always present (`false`
  except "Add a company").
- **Companies.** `{companies: CompanyRow[], active, openPostings}`: `active` counts companies with
  status `active`, `openPostings` sums the listed rows' `activeJobs`.
- **Profile.** `base` is `{version, createdAt}` of the newest base version whether or not it reads
  (`getBaseResume`); `render` is `{documentId, version}` of the newest base version that has a
  PDF of the same user (`latestBaseRender`); the frontend derives `current` from
  `render.version === base?.version`.
- **Settings.** `{user: {name, email}, settings: SettingsOut, review: {perItemUsd, model},
  spend: {usd, calls, inputTokens, outputTokens}, byPurpose, byModel, byDay, monthStart,
  aggregator: {used, budget, enabled}, purposes: [{purpose, ref, error, reasoning, rate: {input,
  output} | null}], providers: {present}}`. `monthStart` is a datetime (`2026-10-01T00:00:00Z`);
  `byDay[].day` is a date. The month's totals are the per-purpose sums (exact numeric sums, one
  float conversion), so the page reads four queries. Provider keys never appear: only which
  providers have one (`test_settings_page_shows_key_presence_only`).
- **Ordering ties** get a stable second key: runs `-created_at, -id`; companies `name, id`;
  spend rows by usd desc, then purpose or provider and model.

## 5. The companies page reuses 5C's row, not its per-company reads

- **What.** `build.companies_page` reads the companies (only the columns `company_row` needs, so
  never the careers-page `listing_cache`) with `Count("jobs", filter=Q(jobs__is_active=True))` in
  one query, and builds each row with 5C's `company_row` into 5C's `CompanyRow`.
- **Why.** `row_for` and `active_job_count` are per company (two reads each): 452 reads for the
  live mirror of 226 companies, against a budget of 3. The annotation counts exactly what
  `active_job_count` counts. `test_companies_page` asserts every row equals `row_for`'s.
- **Evidence.** Live on `foray_app` (226 companies, 27,284 active jobs): 200 in 86 ms, 223 active,
  27,259 open postings (the rest have no company: aggregator postings).

## 6. The tenancy sweep, precisely (R8)

- **What.** `tests/pages/test_tenancy.py` (transactional: the chat view claims its conversation on
  a `db()` thread, which needs committed rows):
  - `ROUTES` lists every per-user path-id route with the A row it is called with; `SHARED` lists
    `PATCH /api/companies/{slug}` and `POST /api/companies/{slug}/refresh`.
    `test_every_path_id_route_is_listed` compares both with the schema's path-id operations.
  - `test_tenancy_sweep`: as B, every route with A's id is 404 with `ok:false`, unsafe ones with a
    readable body (so the refusal cannot be a 400), and A's matches, runs, approvals, events,
    conversations, messages, documents and resumes are unchanged. Every GET without a path id
    (read from the schema: `/api/live?since=0`, `/api/matches?ids=<A's>`, `/api/conversations`,
    `/api/me`, every `/api/pages/*`, …) answers B with none of A's ids or texts, and answers A
    with some (except health, auth config and the shared companies page). `POST /api/chat` with
    A's conversation id is 404 "That chat does not exist."; `POST /api/resumes/tailor
    {matchId: A's}` is 404 and starts nothing.
  - `test_the_owner_reaches_every_route`: A gets 200 (202 for evaluate) on every listed route, so
    B's 404 is a refusal, not a wrong id. `test_shared_routes_reach_the_mirror` and
    `test_a_malformed_id_is_404_on_every_route` complete it.
- **Evidence (mutations, each reverted).** The match page read through `unscoped(...)`: the sweep
  fails "GET /api/pages/matches/{id} answered 200". The activity page's runs unscoped: "
  /api/pages/activity shows B what is A's". A scratch `GET /api/pages/scratch/{id}`:
  `test_every_path_id_route_is_listed` fails.

## 7. Key-shape snapshots

- **What.** `tests/pages/snapshots/<page>.json` hold each page's answer for the full graph reduced
  to keys and JSON types (arrays list their items' distinct shapes). `FORAY_UPDATE_SNAPSHOTS=1`
  rewrites them; `test_snapshots_are_all_used` keeps the set equal to the pages.

## 8. The generated files are staged, not committed

- **What.** `backend/openapi.json` and `src/lib/api/schema.d.ts` were regenerated with `pnpm
  api:types` and staged (`git add`), so the drift step's `git diff --exit-code` compares a fresh
  regeneration with them; the verifier commits them with the step.
- **Evidence.** `pnpm api:types` exit 0, then `git diff --exit-code backend/openapi.json
  src/lib/api/schema.d.ts` exit 0; `tsc --noEmit` and `eslint` exit 0 against the new types.

## 9. Handoffs still open

- `tests/accounts/test_auth.py::test_sliding_session_rules` (step 3A's file) still asserts the
  chat stub's 503 at line 515; it is the one failure of the full suite (step-6.md §4): assert 400.
- Chat tool **input** models are not registered for OpenAPI (step-6.md §3): `chat/api.py` is step
  6's. Registering `INPUT_MODELS.values()` also needs their names in `SNAKE_CASE_COMPONENTS`
  (tool arguments keep snake_case). Step 8D's `chat-types.ts` needs them; the next backend owner
  (8E) regenerates the types after.
