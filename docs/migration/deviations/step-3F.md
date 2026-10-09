# Step 3F: deviations and clarifications

Later steps read this before the design. Each entry: what, why, evidence.

## 1. Write order: every table after the tables its foreign keys name

- **What.** The importer writes users, companies, jobs, job_matches, documents (and their
  files), resumes (then the `tailored_resume` second pass), conversations, runs, approvals,
  events, **messages**, llm_calls, api_usage. §3.3 lists messages before runs. As the first
  statement of the import transaction it runs `SET CONSTRAINTS ALL IMMEDIATE`.
- **Why.** Django creates foreign keys `DEFERRABLE INITIALLY DEFERRED`, so a dangling
  reference would surface only at commit, as one error that names no row. With the constraints
  immediate, a bad reference fails at its own batch, and the row-by-row retry names it, as §3.3
  asks ("a hard failure naming the row"). `messages.run_update_event` names an event, so
  messages must follow events.
- **Evidence.** Before the change, `test_replace_gives_identical_checksums` failed with
  `messages row 3: … violates foreign key constraint "messages_run_update_event_id_…_fk_events_id"`
  (the earlier import in the same test transaction had made the constraints immediate).
  `test_hard_failures_roll_everything_back[a CHECK the new schema adds]` shows the named-row
  failure: `companies row <id>: new row for relation "companies" violates check constraint
  "companies_slug_shape"`.

## 2. Checks beyond §3.4's list (additions)

- **Check 11 "Other columns"** compares every mapped column of companies (`listing_cache` must
  be `{}`), events (approval kept only when it exists), conversations (`turn_started_at` NULL),
  messages (`seq` in `created_at, id` order, `run_update_event`, `updated_at = created_at`) and
  llm_calls (`conversation` and `label` NULL). With checks 1–10 this proves every mapping rule
  of §3.3 at import time, not only in tests.
- **Stricter than written:** the 200-job sample compares every column (plus sha1 of
  `description_raw`, `description_text` and `raw`) and that `content_hash` and `dedupe_key` equal
  the Python recomputation; spend is compared per `(purpose, provider, model)`; the match tuple
  includes `tailored_resume_id` (NULL where the legacy one dangled); documents compare every
  column and `render_hash` must be NULL.
- **JSON** is compared as Postgres renders `jsonb` (`col::text`), on both sides. That text is
  canonical (keys normalised, numbers as stored), so it is "canonical-JSON-equal" without a
  Python round trip that could hide a float that did not survive.
- **A check that cannot run** (a query error) counts as failed.

## 3. Backup details (`backup_legacy`)

- **One snapshot.** The counting transaction (`REPEATABLE READ READ ONLY`) exports its snapshot
  and `pg_dump --snapshot=<id>` reads exactly it, so the marker's counts are the dump's counts
  even with a writer present. The marker format is step 0's (deviations/step-0.md §1), byte
  layout included (`indent=2`, trailing newline); the manifest is `<sha256>  ./<path>` per file,
  sorted by path, which is byte-identical to step 0's manifest for the live tree.
- **Refusals.** A `BACKUP_DIR` inside `LEGACY_STORAGE_DIR` is refused before anything is
  created; a stamp that already exists is refused; a failed step deletes that stamp's files,
  so a backup without its marker never lingers. `--container` defaults to `foray-db-1`.
- **The importer checks the marker's dump too**: it must exist and hash to `dump_sha256`
  (a marker without its dump is not a backup). The 24-hour age limit is §3.1's; `--rehearsal`
  lifts only that.

## 4. Preflight additions

- `STORAGE_DIR` inside `LEGACY_STORAGE_DIR` (or the reverse) is refused: copying would write
  into the legacy tree.
- Order: configuration (no queries) → target guard (`assert_app_database`, then the `legacy`
  rows of `django_migrations`) → source guard (read-only session, `SHOW transaction_read_only`,
  the two legacy tables) → marker → cutover / non-empty. `test_refuses_swapped_urls` records
  every statement: with the URLs swapped the only one sent is the guard's `LEGACY_MARKERS_SQL`,
  and the source is never opened.
- More than one credential account for a user, an `api_usage.id` that is not
  `provider:period`, and an approval whose `continuation` has no `queue` are hard failures (no
  repairs). The refusal for a non-empty target names the database and the tables with rows:
  "foray_app already has data; pass --replace or run reset_app_db (rows in …)".

## 5. Mapping choices the design left open

- `users.updated_at` is the legacy `user.updated_at` (naive UTC → aware), like `created_at`.
  `last_login` is NULL; `is_active` true.
- A converted (pending → expired) approval keeps `decided_at` as it was (NULL): §3.3 names only
  `status` and `decided_via`, and `expire_lapsed` (§9.6) does not set it either. A converted run
  keeps `updated_at`; `finished_at` is the import's start time.
- Check 10 compares a converted run's `finished_at` as "converted" on both sides (the legacy
  value is NULL, the new one is the import time); check 8 then requires it to be set.
- `messages.updated_at = created_at` (legacy messages have none); `conversations.turn_started_at`
  is NULL; JSON `null` (as opposed to SQL NULL) would be written as JSON `null`
  (`Value(None, JSONField())`); the live data has none.

## 6. `--verify-only` and `verify_import`

- Check 9 compares today's legacy fingerprints and file fingerprints with those the import
  recorded (`checksums.fingerprints`, `checksums.legacy_files`), and today's legacy counts with
  the marker the import used (looked up by name in `BACKUP_DIR`; a missing marker fails check
  9).
- The checks describe the database *as imported*. They are meant for step 12's items 4–6, before
  anyone signs in and before the worker runs: the first sign-in replaces the hash and bumps
  `users.updated_at` (checks 7 and 10 then fail, `test_golden_password_signs_in_and_upgrades`
  asserts exactly `{7, 10}`), and the next refresh changes jobs.
- `--password-prompt` checks the one user that still has a `ba_scrypt$` hash (`--email` picks
  one when there are several), refuses without a terminal, prints exactly "Password matches the
  imported hash." or "Password does not match." (exit 1), and saves nothing.

## 7. `reset_app_db` and the rehearsal's scratch side

- `reset_app_db`: DEBUG only; name allowlist `{foray_app, foray_rehearsal}`, then
  `assert_app_database`, then the cutover check, all before any connection to `postgres`. It
  closes Django's connections and pool, `DROP DATABASE` **without** `FORCE` (a database a dev
  server or worker is still connected to is refused by Postgres: stop them first), `CREATE
  DATABASE`, then `migrate`. `STORAGE_DIR` is not touched (step 12 moves it aside itself).
- **New command `legacy_rehearsal {restore,drop} [--store DIR]`** (`apps/legacy/rehearsal.py`):
  `restore` restores the newest marker's dump into `foray_restore` (digest checked, restored
  counts must equal the marker's), creates an empty `foray_rehearsal` and optionally a scratch
  store marked with `.foray-rehearsal-store`; `drop` drops exactly those two databases (allowlist
  `{foray_rehearsal, foray_restore}`, not forced) and removes the store only if it carries the
  sentinel and overlaps neither `STORAGE_DIR` nor `LEGACY_STORAGE_DIR`.
- **Why.** §3.1 requires the rehearsal cleanup to go through a name allowlist; a command makes
  that allowlist code rather than a manual `dropdb`, and makes the rehearsal repeatable.

## 8. Tests

- The fixture database is named after pytest's own: `test_foray_app` → `test_foray_legacy`,
  `test_foray_app_<lane>` → `test_foray_legacy_<lane>`, so parallel lanes never share it. It is
  built once per session from `schema.sql` plus `tests/legacy/seed.py`, and every test imports
  from its own `CREATE DATABASE … TEMPLATE` copy, so tests that corrupt the legacy side stay
  independent. The seed has 1,156 jobs (more than one 1,000-row batch and the 200-job sample).
- The `reset_app_db` name-allowlist case of §12.3 is `tests/legacy/test_commands.py` (step 1's
  `core/test_guards.py` is not this step's file). No test drops a real database; the success path
  ran live on `foray_rehearsal` (§9).
- `test_backup_legacy_end_to_end` runs the real `docker exec foray-db-1 pg_dump` against a copy
  of the fixture database and is skipped when the container is not reachable.

## 9. The rehearsal: how it ran

- **Where the files went.** `BACKUP_DIR` and the scratch `STORAGE_DIR` were directories inside
  this lane's worktree (`backups/rehearsal-3F`, `storage/rehearsal-3F`, both git-ignored), set
  on the command line, instead of the main checkout's `backups/django-migration/`: the lane may
  write only inside its worktree. The rehearsal's dump (a copy of the real data) was deleted
  after the run; the step 0 backup in the main checkout is untouched.
- **`git -C <main checkout> status`** could not be run: the lane sandbox refuses every git
  command aimed at the main checkout. In its place, a read-only listing of every file in the
  main checkout outside `.git`, `node_modules`, `.next` and `.claude` (path, size, mtime) was
  hashed before and after: 325 files, `30ed029c…` both times. The `data/` sha256 manifest was
  byte-identical to step 0's and to the new backup's before and after. The legacy `foray` write
  counters stayed at 41158/1014040/5351 throughout.
- **Timings.** Backup 11 s; restore into `foray_restore` 21 s; the import 32.8 s (34 s wall);
  `--replace` re-import 32.9 s; `verify_import` 1.0 s.
- **Results.** 35,807 legacy rows in 18 tables, equal to the marker; imported users 1,
  companies 227, jobs 33,824, job_matches 284 (archived 176, found 98, shortlisted 10),
  documents 2, resumes 2, runs 2, approvals 2, events 32, llm_calls 1,410 ($2.609684 on both
  sides), api_usage 1; 0 conversions, 0 reported rows, no active pg-boss jobs. Every check passed,
  10 included. Both files copied, each hashing to its `documents.sha256`
  (`e914a8a3…`, `b6499260…`).
- **Hash parity.** The Python `content_hash` and `dedupe_key` equal the values the TypeScript
  stored for all 33,824 live jobs, so the first Python refresh will see unchanged postings as
  unchanged.
- **Extra live checks.** The swapped pair (`DATABASE_URL=…/foray_restore`,
  `LEGACY_DATABASE_URL=…/foray_rehearsal`) was refused by the target guard with and without
  `--replace`; both databases' fingerprints were identical before and after. A `--replace`
  re-import gave a second record with identical checksums. `reset_app_db` on `foray_rehearsal`
  dropped, recreated and migrated it (0 users, 0 jobs, 0 import records afterwards); on
  `foray_restore` it was refused by the allowlist. `legacy_rehearsal drop --store …` then dropped
  both databases and removed the store.

## 10. Observed, not changed (handoff)

- `tests/accounts/test_commands.py::test_create_test_user_purge` (step 3A's file) compares
  `sorted(names)` with the unsorted list `[keep.pk, "unrelated.txt"]`; it fails whenever the
  random user id sorts after `unrelated.txt` (an id starting `v`–`z`, about 8% of runs). It failed
  once in this lane's full-suite run and passed on the re-run. Fix: compare with
  `sorted([keep.pk, "unrelated.txt"])`.
