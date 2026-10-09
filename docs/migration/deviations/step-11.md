# Step 11: deviations and clarifications

Later steps read this before the design. Each entry: what, why, evidence.

Step 11 owns the repo-wide deletions, `DESIGN.md`, `CLAUDE.md`, `README.md`, `.env.example`,
`package.json` (with `pnpm-lock.yaml`), `vitest.config.ts`, the register-source switch in
`backend/tests/test_register.py`, and the generated `backend/openapi.json` and
`src/lib/api/schema.d.ts` (regenerated; no drift, so unchanged).

## 1. What was deleted

- **§6.7's list:** `src/app/actions/**` (8 files), `src/worker/**`, `src/env.ts`; under
  `src/lib`: `agents`, `auth`, `chat/follow-up.ts`, `companies`, `db`, `llm`, `load-dotenv.ts`,
  `pool.ts`, `profile`, `queue`, `ranking`, `resume`, `runs`, `sources`, `storage`, `system`;
  `scripts/*.ts` and `scripts/capture-goldens.mts` (step-0.md handoff); `drizzle/**`,
  `drizzle.config.ts`, `.cx.ts`; every `src/test/unit/*.test.ts` except `format.test.ts`,
  `src/test/setup.ts`, `src/test/fixtures/**`; `src/components/ai-elements/code-block.tsx`.
  166 tracked files in all (`git rm`, so the deletions are staged).
- **Checked before deleting:** every file under `src/test/fixtures/` (except
  `resumes/plain.docx.gen.mjs`, as §6.7 says) is byte-identical to its copy in
  `backend/tests/fixtures/`, and `src/lib/companies/seed.json` to `backend/apps/jobs/seed.json`.
- **`src/components/companies/company-row.ts`** (step-8C.md §1): `toCompanyRow`, the legacy
  `Company` import and `BOARD_LABEL.none` are gone. `BOARD_LABEL` is now
  `Record<CompanyRow["sourceType"], string>` over the generated `SourceType`
  (`greenhouse | lever | ashby | careers_page`), so `tsc` checks that every source has a label.
- **npm dependencies (§1.6):** `better-auth`, `@better-auth/drizzle-adapter`, `@better-auth/cli`,
  `drizzle-orm`, `drizzle-kit`, `pg`, `pg-boss`, `playwright-core`, `unpdf`, `mammoth`,
  `@napi-rs/canvas`, `@ai-sdk/{anthropic,deepseek,google,openai,openai-compatible}`,
  `@t3-oss/env-nextjs`, `nanoid`, `tsx`, `docx`, removed with `pnpm remove` (the lockfile
  shrank by about 2,560 lines; nothing was downloaded). `ai` and `@ai-sdk/react` stay.
- **`vitest.config.ts`:** `setupFiles` is gone (`src/test/setup.ts` imported the deleted
  `lib/load-dotenv`); `include` is unchanged and now matches only `format.test.ts`.

## 2. Four small edits outside the step's named files (consequences of the deletions)

- **`eslint.config.mjs`:** the "Architecture boundaries" block (Next must not import Playwright
  or `src/worker`; the worker must not import the app or Next) is removed. §1.5 says "the old
  worker and Playwright boundaries go away with the code", and step-3G.md §1 left them for
  step 11. The `"use client"` → `lib/api/server` rule is the one boundary left.
- **`next.config.ts`:** `serverExternalPackages: ["@napi-rs/canvas"]` (and its comment) is
  removed with the dependency; it served the deleted PDF-image fallback.
- **`Dockerfile`** (step 9 owns it): the web build stage's `ENV SKIP_ENV_VALIDATION=1` and its
  comment ("env.ts validation is skipped at build time; it goes with the legacy server code
  (step 11)") are removed. Only `src/env.ts` (`@t3-oss/env-nextjs`) read the variable, and
  both are deleted here; `git grep SKIP_ENV_VALIDATION` outside `docs/migration` now finds
  nothing, and `pnpm build` passes without it. Added after verification round 1.
- **`@types/pg`** was removed with `pg`. §1.6 does not name it, but it types only the removed
  package.
- **Why these count as the step's.** Each one names only code this step deletes; leaving them
  would describe modules that no longer exist. Nothing else in these files changed.

## 3. Kept on purpose

- `zod` stays: `ai` 7 declares it as a peer dependency (`node_modules/ai/package.json`).
- `shiki` stays although only the deleted `code-block.tsx` imported it: §1.6 does not list it,
  and `@streamdown/code` brings its own copy, so removing it is a separate clean-up.
- `pnpm-workspace.yaml`'s `onlyBuiltDependencies: [esbuild]`, `tsconfig.json`'s `**/*.mts`
  include and `.gitignore`'s `/.*.ts` rule are harmless with no file to match.

## 4. The register moves to DESIGN.md under `## Invariant register`

- **What.** `test_register.py` reads `REPO_ROOT / "DESIGN.md"` and splits on
  `REGISTER_HEADING = "## Invariant register"`, because DESIGN.md has no numbered sections
  (the migration design's heading was `## 0. Invariant register`). Its docstring no longer
  describes the pre-step-11 state. The table is §0's 17 rows verbatim, except that R16 drops
  its `(§3.1)` citation, which would dangle once step 13 deletes `docs/migration/`.
- **Evidence.** `FORAY_REGISTER_STRICT=1 pytest tests/test_register.py`: 27 passed. With
  `test_start_is_atomic` renamed in DESIGN.md (a scratch edit, reverted byte for byte):
  `FAILED test_register_row_is_proved[R1]`, 1 failed, 26 passed.

## 5. DESIGN.md: how the D-rows of §14 were applied

The D-rows are applied in the text (the Stack row, the 17 invariants restated for Python,
Architecture, Interfaces, LLM layer, Known risks), and DESIGN.md ends with a short
"Corrections made with the Django migration" table naming each of D1–D10 and what replaced it,
so a reader can see what changed. The `render_hash` rule includes the tailored-resume
extension of step-H1.md §10:
`sha256(html_normal | RENDER_VERSION | "<Name> - Resume - <Company>.pdf")`, checked against
`apps/resumes/render.py` `render_hash`/`_hash_html`.

## 6. The real legacy user id is redacted from the map copy

`docs/migration/map/tests-scripts-infra.md:421` quoted the id hard-coded in `.cx.ts`; it now
reads "(redacted)". `git grep` for the id finds nothing in the tree (git history still has
it). The scratchpad copies of the maps are outside the worktree and were not touched.

## 7. Handoff (step 13)

- `backend/tests/chat/test_uistream.py` (step 3E's) reads
  `docs/migration/prototypes/ts-turn{1,2}.{sse,persisted.json}`
  (`::test_accumulator_reproduces_ts_reference_turns`). When step 13 deletes `docs/migration/`,
  that test fails unless those four files move first (for example to
  `backend/tests/goldens/uistream/prototypes/`) and `PROTOTYPES` follows them.

## 8. Checks run in this step

- **The Done grep.** `git grep -nE "drizzle|pg-boss|better-auth|@/lib/db|requireUser|@/env|load-dotenv" -- src scripts vitest.config.ts`
  prints nothing (exit 1; `scripts/` no longer exists).
- **`pnpm check`** with `FORAY_REGISTER_STRICT=1 FORAY_TEST_DB_NAME=test_foray_app_step11`:
  exit 0. ESLint clean; ruff "All checks passed!", 271 files formatted; `tsc` clean; pyright
  0 errors; Vitest 1 file, 16 tests; pytest **1710 passed** (no skips, the one third-party
  `google.genai` DeprecationWarning); `pnpm api:types` then `git diff --exit-code
  backend/openapi.json src/lib/api/schema.d.ts` clean (no backend change, so no drift).
- **`pnpm test:web` runs `format.test.ts` only:** `vitest list --filesOnly` prints exactly
  `src/test/unit/format.test.ts`.
- **`pnpm build`:** exit 0, 13 routes, no warning or error line.
- **Pane smoke test** (`LLM_FAKE=1 JSEARCH_ENABLED=false pnpm dev:all` in its own process
  group; the pane opened with `preview_start {url: http://localhost:3000/sign-in}`): a
  `create_test_user --with-resume` user and a `pnpm dev:session` cookie (`cookie:
  sessionid=<key>`) set with `document.cookie`; `/api/me` 401 before, 200 after.
  - `/home` rendered "Ready when you are.", the suggestions, "Best matches / No open matches
    yet", the board counts and "This month / Model spend $0".
  - `/chat`: "What's on my resume?" typed into the focused composer and sent with Enter. The
    address became `/chat/<id>` without a reload (a window marker survived), and the reply
    showed the "Read your resume" tool line and "Here is the top of your resume: Name: Avery
    Quinn.". The ledger has 2 `chat` rows, `label='fake'`, $0, with the conversation id.
  - Console errors: only the deliberate 401 of the check's own `fetch('/api/me')` before the
    cookie was set. The api log has no 4xx/5xx besides those signed-out `/api/me` probes.
- **Afterwards.** The viewport emulation was reset and the pane's tab closed. SIGINT to the
  process group: the worker logged `stopping` then `stopped`, uvicorn "Finished server
  process", `next dev` exited 0; :3000, :8000 and :8001 are free and no dev process remains.
  `create_test_user --purge` printed "Deleted 1 test user."; `foray_app` has 0 users,
  0 sessions, 0 runs, 0 conversations, 0 worker rows and 0 todo/doing jobs.
- **Money and legacy.** $0 (fakes only). The legacy `foray` write counters were
  41158/1014040/5351 before and after.
- **Re-run after verification round 1** (the `Dockerfile` line above, and DESIGN.md's app
  count corrected from "nine" to "ten" to match the ten names it lists and `INSTALLED_APPS`):
  `pnpm build` with no `SKIP_ENV_VALIDATION` in the environment, exit 0, 13 routes;
  `FORAY_REGISTER_STRICT=1 pytest tests/test_register.py`, 27 passed;
  `docker compose -p foray --profile prod config --quiet`, exit 0. The build stage's only env
  reads (`next.config.ts`, `src/lib/api/server.ts`: `DJANGO_ORIGIN`) have defaults, and
  `.dockerignore` keeps `.env` out of the image, so nothing else depended on the skip.
- **Test database for the orchestrator to drop:** `test_foray_app_step11`.
