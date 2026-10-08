# Step 0: deviations and clarifications

Later steps read this before the design. Each entry: what, why, evidence.

## 1. The backup marker format, pinned for step 3F

- **What.** `BACKUP_DIR/<stamp>.ok` holds exactly `{dump, dump_sha256, counts, created_at}`:
  - `dump` is the dump's file name relative to `BACKUP_DIR` (`<stamp>-foray.dump`), not a path;
  - `counts` maps every `public` table name to its row count (all 18 tables, `json_object_agg` in a
    `REPEATABLE READ READ ONLY` transaction);
  - `created_at` is ISO 8601 UTC with a `Z` (`2026-10-08T21:21:34Z`).

  `<stamp>` is `date -u +%Y%m%dT%H%M%SZ`. Besides the dump, the tarball, the manifest and the
  marker, the directory also holds `<stamp>-foray.list` (the `pg_restore --list` output that was
  checked).
- **Why.** §3.1 says step 3F's `backup_legacy` writes "the same format" but does not spell it out.
- **Evidence.** `backups/django-migration/20261008T212125Z.ok`; counts were equal before and
  after the dump and equal a fresh `BEGIN READ ONLY` count (jobs 33,824, llm_calls 1,410,
  session 17: the live data has grown since the design's numbers).

## 2. Better Auth hashes are kept across re-runs

- **What.** `scripts/capture-goldens.mts` mints the two golden hashes with the installed
  `@better-auth/utils` (0.4.2, resolved from `better-auth` 1.7.5) on the first run, and on later
  runs keeps the stored hash when it still verifies with that library (it re-mints otherwise).
- **Why.** `hashPassword` uses a random salt, so a literal re-mint can never satisfy "re-running
  the capture script gives `git diff --exit-code backend/tests/goldens`".
- **Evidence.** Two re-runs (one under `TZ=UTC`) left all 109 golden files byte-identical; the
  Done check's `node` one-liner and a Python `hashlib.scrypt` both verify both hashes.

## 3. Adapter goldens pin `descriptionRaw` by hash

- **What.** `goldens/adapters/**` hold every `NormalizedJob` field except `raw` (the fixture
  payload itself), and `descriptionRaw` is `{sha256, utf8Bytes}` of the exact string instead of
  the string. `descriptionText` and everything else are stored in full.
- **Why.** The step's additions had to stay well under 5 MB; the raw HTML alone was 200 kB and
  is reproducible from the fixtures (verbatim for Greenhouse and Ashby, stitched from the
  fixture's fields for Lever).
- **Evidence.** All step 0 additions are 3.5 MB raw (1.2 MB gzipped), of which the goldens are
  0.8 MB; most of the rest is the fixtures (1.5 MB, PDFs and careers pages) and the docs (1.3 MB).

## 4. How the capture script reaches TypeScript code that reads the database

- **What.** Two captured paths read Postgres inline: the fast-score prompt (built inside
  `scoreMatches`) and `get_job`'s salary string. The script replaces the pool's `query` with an
  in-memory fake that answers their SELECTs from fixed rows (numeric salaries as the strings
  Postgres returns), and points `DATABASE_URL` at a closed port. No database is read or written.
  Model calls go to a capture model installed on `registry.languageModel`; it records the
  `LanguageModelV4` prompt and throws, so nothing reaches a provider ($0 spent).
- **Why.** Step 0 must not write `foray`, and the builders are not factored as pure functions.
- **Evidence.** `goldens/prompts/fast_score/missing_max_no_resume.json` shows TS's
  `Pay: 150000–null` (the §8.10 declared difference); `goldens/prompts/tool_salary/get_job.json`
  shows `USD 124.128–130.5 per hour`.

## 5. UI-stream reference turns

- **What.** `goldens/uistream/` holds 12 scenarios run through `ToolLoopAgent` +
  `createAgentUIStreamResponse` exactly as `src/app/api/chat/route.ts` calls it, with scripted
  `MockLanguageModelV4` models and three stand-in tools (`list_jobs`, `get_job`, `tailor_resume`),
  not the real `chatAgent` (it needs auth and the database). The research's two approval turns
  are re-captured as `approval_turn{1,2}` with a seeded `Math.random`, so their approval id
  differs from the research copies in `docs/migration/prototypes/ts-turn{1,2}.*`.
- **Why.** Determinism (the SDK draws ids from `Math.random`), and isolation from auth and data.
- **Evidence.** The route passes no `onError`, so every error the TS route streams reads
  "An error occurred." (`tool_raises`, `invalid_tool_input`, `unknown_tool`, `stream_error`). A
  tool returning `undefined` streams `"output":null` (`tool_returns_undefined`). Python tests that
  emit §7.2/§7.3 texts instead declare those as `ALLOWED_DIFFS`.

## 6. Prototype paths

- **What.** The prototypes are copied flat into `docs/migration/prototypes/` (the paths §7.3 and
  the design's preamble cite), not into `py/`, `node/` and `out/` subfolders.
- **Why.** Citations resolve as written. `test_accumulator.py` imports `server_stdlib` and reads
  `../out/`, which were not part of the step's list; it is kept as a reference, not run.
- **Evidence.** `docs/migration/prototypes/{uistream,patched_stream,test_accumulator}.py`,
  `{parse_stream,repo}.mjs`, `ts-turn{1,2}.{sse,persisted.json}`.

## 7. Worktree `.env`

- **What.** Built from the main checkout's `.env`: `BETTER_AUTH_*` removed; `DATABASE_URL` now
  names `foray_app`; `STORAGE_DIR` is the absolute `<main>/storage`; added `LEGACY_DATABASE_URL`
  (`…/foray`), `LEGACY_STORAGE_DIR` (`<main>/data`), `BACKUP_DIR`
  (`<main>/backups/django-migration`), a new 86-character `SECRET_KEY`, `DEBUG=true` and
  `DJANGO_ORIGIN`. The empty `GOOGLE_CLIENT_*` lines stay (env.py ignores them).
- **Why.** The step names only `BETTER_AUTH_*` for removal. The main checkout's own `.env` is not
  changed (its `STORAGE_DIR` still serves the legacy app; step 13 handles it).
- **Evidence.** Key names only: `sed 's/=.*/=<set>/' .env`.
