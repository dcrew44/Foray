# Group G1 integration: deviations and clarifications

Later steps read this before the design. Each entry: what, why, evidence.

G1 is steps 3A, 3B, 3C, 3D, 3E and 3G. Each lane's single commit was cherry-picked onto
`claude/middleware-django-migration-389b1a` in that order. **No conflicts**: the lanes own
disjoint files (§15.1), so there was nothing to resolve.

## 1. `budget_exceeded()` builds the real `BudgetExceeded` with its real constructor (fix)

- **What.** `backend/tests/contract/scenarios.py` (step 3E's file): `budget_exceeded()` now
  returns `apps.llm.budget.BudgetExceeded(Decimal(90))` and checks that its `user_message` is
  `BUDGET_TEXT`. The stand-in class and the `importlib` fallback are gone.
- **Why.** §8.6 writes the refusal as `BudgetExceeded(user_message="…$90…")`, which step 3E took
  literally. Step 3B's class takes the budget and writes the sentence itself
  (`BudgetExceeded(budget: Decimal)`; `user_message` is the attribute §8.6 asks for). Once both
  landed, the helper raised `TypeError`, as 3E said it would. The sentence 3B writes for $90 is
  byte-equal to `BUDGET_TEXT`, so the fix only changes how the exception is constructed. The
  stand-in would now only hide a future change to the class, so it was removed.
- **Evidence.** Before: 3 failures, `tests/chat/test_uistream.py::test_function_model_turns[agent_budget]`,
  `::test_agent_error_text` and `tests/contract/test_contract.py::test_function_model_agent_validates`
  (`TypeError: BudgetExceeded.__init__() got an unexpected keyword argument 'user_message'`).
  After: `tests/chat` and `tests/contract` 106 passed. The contract scenario `agent_budget` now
  streams the real exception through `ForayVercelStream`, and the Node script accepts it with that
  error text.

## 2. Handoff applied: `Preferences` is an allowed snake_case component

- **What.** `backend/tests/core/test_wire_format.py` (step 1's file):
  `SNAKE_CASE_COMPONENTS = {"Preferences": "§2.1: preferences keep today's snake_case keys"}`.
- **Why.** Handoff 1 of step 3A (deviations/step-3A.md §1). `PUT /api/preferences` takes the
  stored snake_case `Preferences`.
- **Evidence.** The full suite passes: 1144 passed, 15 skipped, 10 xfailed. The skips are the 13
  pending register rows, the missing `apps/chat/tools.py` and the profile page. The xfails are
  the 10 strict ones in `tests/llm/test_jsonio.py`.

## 3. `backend/openapi.json` and `schema.d.ts` are left as step 3G wrote them

- **What.** The committed `openapi.json` holds only `/api/health`. With 3A merged, the export
  also has `/api/auth/{config,sign-in,sign-out,sign-up}`, `/api/me`, `/api/preferences` and
  `/api/settings`. The integration did not regenerate the file.
- **Why.** §5.4: the generated files are committed only by steps 3G, 7, 8E and 11. 3G's copy is
  the deliberate stub-router copy that keeps `tsc` green. So `pnpm check`'s final
  `git diff --exit-code` step fails until step 7 regenerates. `tsc` and `eslint` pass.

## 4. The fake careers-page check ran in a scratch database

- **What.** The `LLM_FAKE=1 seed_companies --file tests/fixtures/careers_company.json --fetch`
  check ran against a throwaway `foray_app_g1fake`. It was created, migrated and then dropped.
  The real check ran against `foray_app`.
- **Why.** A fake extraction stores its result in `companies.listing_cache`. In `foray_app` that
  entry would be a cache hit for the real run, which then never calls the model. It would also
  put 25 fake-derived postings into the dev mirror.
- **Evidence.** In the scratch database: 1 model call, 25 postings inserted, and 1 ledger row
  `extract|fake|fake|$0`. A second fake run made 0 model calls and found 25 unchanged.

## 5. The proxy check's scratch page sat outside `(app)`

- **What.** The deferred 3G check used `src/app/(g1probe)/settings/g1-probe/page.tsx`. That page
  logged `x-foray-path` and called `getMe()`. It was deleted after the check, and
  `/settings/g1-probe` then answered 404.
- **Why.** `src/app/(app)/layout.tsx` still calls the legacy `requireUser()` (Better Auth). That
  call would act before any page under `(app)` could render. The `/settings/:path*` matcher
  still applies to the new group, so `proxy.ts` runs exactly as it does for real pages.

## 6. Observations, no change

- `create_test_user --purge` deletes the `@example.test` users. Their `django_session` rows stay
  behind, and Django treats those sessions as anonymous. After the purge, the integration
  deleted the 2 orphaned sessions that the curl checks had created.
- The 10 strict xfails in `tests/llm/test_jsonio.py` (deviations/step-3B.md §9) remain. They are
  not caused by combining the lanes: `apps/core/jsonio.py` is step 1's. The next step that owns
  that file fixes `js_num`/`js_locale` and removes the markers.

## 7. Checks run on the combined code

- **Static checks.** `uv sync`, `pnpm install --frozen-lockfile --prefer-offline` and
  `pnpm db:migrate` (no migrations to apply) all ran. pytest gave 1144 passed, 15 skipped, 10
  xfailed. The render tests ran on local Chrome and the contract tests ran in Node. `ruff check`
  and `ruff format --check` were clean, pyright reported 0 errors, and `tsc --noEmit` and
  `eslint` both exited 0.
- **3A, through :3000** (`pnpm dev` + `pnpm dev:api`):
  - Sign-up, `/api/me`, sign-out, `/api/me`, sign-in and `/api/me` answered
    200, 200, 200, 401, 200 and 200.
  - A wrong password answered 400 `{"ok":false,"message":"Invalid email or password"}`.
  - A POST carrying the cookie was refused with 403 "Cross-site request refused." in three cases:
    `Origin: http://evil.example`, `Sec-Fetch-Site: cross-site` with an allowed Origin, and no
    Origin or Referer at all. The session stayed valid afterwards (`/api/me` 200).
  - Same-origin requests with no token all answered 200: `PUT /api/preferences` (user with no
    resume: "Preferences saved"), `PATCH /api/settings` and `POST /api/auth/sign-out`. No
    `csrftoken` cookie was set.
  - Wrong passwords for a throwaway email: attempts 1–10 answered 400 and the 11th answered 429
    "Too many attempts. Try again in a few minutes.". Another email signed in (200).
  - A `pnpm dev:session` cookie for a `create_test_user` user worked with `curl -b`. With
    `X-Foray-SSR: 1` the answer was 200 with 0 `Set-Cookie`. The same request without the header
    got `Set-Cookie: sessionid=…; HttpOnly; Max-Age=604800; SameSite=Lax`, and a third request
    got none.
  - `create_test_user --purge` printed "Deleted 2 test users.".
- **3G, through :3000:**
  - Requests with no cookie, or with only `better-auth.session_token`, got a 307 to
    `/sign-in?next=%2Fsettings%2Fg1-probe`.
  - A valid `sessionid` with a forged `x-foray-path: //evil.example` got 200. The server
    component logged `x-foray-path=/settings/g1-probe?view=board&stage=applied` and
    `me=<test user>`, which confirms the §15.4 assumption that the header reaches server
    components.
  - A stale `sessionid` makes Django answer 401 to `getMe()`. That request got a 307 to
    `/sign-in?next=%2Fsettings%2Fg1-probe%3Fview%3Dboard%26stage%3Dapplied`.
- **3B with 3C and 3D (fakes):**
  - `LLM_FAKE=1 pnpm llm:smoke` printed 7 ✓ and wrote 7 `fake` rows at $0. The lanes' `extract`,
    `tailor` and `verify` overrides passed on the smoke's schema.
  - `fake.discover()` registered `extract`: `[apps.jobs.fakes.careers_listing_reply,
    apps.resumes.fakes.extract_resume]`, `tailor`: `[apps.resumes.fakes.tailor]` and
    `verify`: `[apps.resumes.fakes.verify]`.
  - Through the real `structured()` stack (registry → budget → ledger), the 3D fakes did this:
    - `extract_resume(single-column.pdf)` gave "Dana Whitlock Reyes" with 3 work items;
    - the vision path gave "Resume from page images";
    - the DOCX gave 3 work items;
    - `tailor_resume` gave 0 changes;
    - `model_judge` gave 3 of 3 supported.
  - Those calls wrote 3 `extract`, 1 `tailor` and 1 `verify` ledger rows, all `label='fake'` at
    $0.
- **3C careers page, live** (real `extract` = `openai:gpt-5.6-luna`, ceiling $0.05):
  - The first `--fetch` registered Retool and found 25 postings, all inserted, with 1 model call.
    It wrote one ledger row, `extract|gpt-5.6-luna|4411 in|1315 out|$0.002460`.
    `needs_browser=f`.
  - The second identical `--fetch` made 0 model calls and wrote 0 new ledger rows. It found 25
    postings: 0 inserted, 0 updated, 25 unchanged. Every job's `last_seen_at` moved, and every
    job still has `description_text`.
  - The postings' `content_hash` covers the text of each posting page. A posting page that was
    not fetched would have empty text and show as updated, so the 25 unchanged postings show
    that every posting page was fetched again. `needs_browser` was still `f`.
  - Real spend: $0.002460.
- **Legacy `foray`:** the write counters stayed at 41158/1014040/5351.
