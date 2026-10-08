# Step 2: deviations and clarifications

Later steps read this before the design. Each entry: what, why, evidence.

## 1. `SHOW idle_in_transaction_session_timeout` prints `1min`, not `60s`

- **What.** The `core.0002_idle_in_transaction_timeout` migration stores exactly
  `idle_in_transaction_session_timeout=60s` (`pg_db_role_setting`), but `SHOW` answers `1min`.
- **Why.** Postgres displays a millisecond setting in its largest exact unit; 60 s is shown as
  `1min`. There is no way to make `SHOW` print `60s`. The intent (60 seconds) holds.
- **Evidence.** `foray_app` and `test_foray_app`: `SHOW` → `1min`;
  `pg_settings` → `60000 ms`; `pg_db_role_setting.setconfig` →
  `{idle_in_transaction_session_timeout=60s}`. `foray` and `postgres` → `0`. The per-database
  settings of every database were identical before and after a `pytest --create-db` run.
  `tests/test_models.py::test_idle_in_transaction_timeout_is_set` checks the stored value.

## 2. `pnpm api:types` needs `openapi-typescript`, which step 3G adds

- **What.** The export half of `api:types` runs as written. The second half was run as
  `pnpm dlx openapi-typescript@7.8.0 backend/openapi.json -o src/lib/api/schema.d.ts`; the
  generated `backend/openapi.json` and `src/lib/api/` were then deleted (not committed).
- **Why.** `openapi-typescript` is not yet a dependency: adding it is step 3G's (`package.json`
  dependencies only). Step 2 does not own `package.json`.
- **Evidence.** `pnpm api:types` → `sh: openapi-typescript: command not found` after writing
  `openapi.json`; the `dlx` run wrote a 1.5 kB `schema.d.ts` with `paths["/api/health"]`.

## 3. Where the JSON-column base lives, and the `choices_check` signature

- **What.** `apps/core/choices.py` holds the column conventions of §2.1 together:
  `choices_check(field, Choices, *, table)` and `choices_checks(table, **fields)` (the
  `*choices_checks` of §2.2), `pairing_checks(prefix)` (returns a one-item list), and the base of
  every JSON column's pydantic model: `JsonModel` (`read(value) -> Self | None`,
  `read_list(value)`, `dump()` = `model_dump(mode="json", by_alias=True)`),
  `DefaultedJsonModel` (`read(value) -> Self`, defaults on anything unreadable), `CAMEL` (the
  config for camelCase-keyed columns) and `JsNumber = int | float` (keeps `150000` an int when
  read and written back, as JavaScript wrote it).
- **Why.** §2.1 says each JSON column has a lenient `Model.read(value)` but names no home for the
  shared base, and step 2 owns only `core/{scoping,choices}.py` in `core`. Django has no table
  interpolation for constraint names (`%(class)s` gives `jobmatch`, not `job_matches`), so the
  table is a keyword.
- **Lenient reads, per column.** `Preferences`, `InterviewState`, `Settings` and `SearchInput`
  fall back to their defaults (today's `safeParse(... ?? {})` with a fallback). Everything else
  (`Resume`, `FastScore`, `DeepEval`, `TailorChange`, `Verification`, outputs, payloads, ...)
  reads as None when missing or unreadable. `read_input/read_output/read_payload(kind, value)`
  in the catalogue dispatch by kind; `jobs.schemas.read_listing_cache` keeps readable entries.
- **Dumping.** `dump()` writes every field, `None` included, as §2.1 says. Pass
  `exclude_none=True` where today's code omits a key; `StoredDecision.dump()` always does
  (`{approved}` with no `itemIds` when the payload has no items).

## 4. Constraints and indexes, precisely

- Every enum column has its `<table>_<column>_valid` CHECK, including the tables whose §2.2 entry
  lists no `*choices_checks` (`documents.kind`, `resumes.kind`, `events.level`, `messages.role`,
  `llm_calls.purpose` and `.label`, `legacy_imports.status`): §2.1 makes it the rule. All
  values were checked against the legacy enums (`drizzle/0000_calm_bloodscream.sql`).
- Every `UniqueConstraint` and plain `Index` uses `fields=[...]` (step 1 deviation 6); the
  descending ones are expression indexes (`F("user"), F("fast_score").desc(nulls_last=True)`),
  which Postgres stores as plain column indexes (`("user_id", "fast_score" DESC NULLS LAST)`).
- §2.2's single-column `Index("company")`, `Index("run")` (approvals, llm_calls) and
  `Index("conversation")` are the foreign keys' own indexes, so they are not declared twice. A
  foreign key that leads a composite index or unique constraint gets `db_index=False` (every
  `user`, `events.run`, `messages.conversation`), as the legacy schema had it.
- Django also adds its default `*_like` pattern-ops indexes on text primary/unique keys
  (`jobs.id`, `companies.slug`) and on the indexed `varchar` `user_id` foreign keys
  (`companies.added_by_id`, `messages.user_id`). Kept: Django's default.
- `ImportRecord.started_at` defaults to now (§2.2's shorthand gave it no type); its `status` is a
  `TextChoices` (`ok | failed`) with a CHECK.
- Field `choices=` take `Enum.choices`, not the class, because `django-types` rejects the class;
  migrations are identical. Foreign keys name the model class wherever there is no import cycle
  (`User` instead of `settings.AUTH_USER_MODEL`; migrations still use the swappable setting);
  the two cyclic ones (`JobMatch.tailored_resume`, `Run.conversation`) are strings with an
  explicit `models.ForeignKey[... | None]` annotation. Concrete models declare their `<fk>_id`
  attributes for pyright.

## 5. `ScopedManager` refuses an empty user and an empty reason

- **What.** `for_user`, `create_for` and `bulk_create_for` raise `UnscopedQueryError` for a falsy
  user id; `unscoped("")` raises `ValueError`.
- **Why.** `filter(user_id=None)` is `IS NULL` in Django, so `LlmCall.objects.for_user(None)`
  would return every system ledger row. The rest is §2.1 verbatim, built on
  `super().get_queryset()` (no `_plain` in the method bodies).

## 6. The kind catalogue: choices the design left open

- **Event kinds.** `EventKind` is exactly the 38 kinds of `map/worker.md` §19: the design adds no
  new event kind (its new behaviour reuses `run.*`, `company.*` and `chat.followup`).
  `DEFAULT_LEVEL` uses the first level §19 lists where it gives two; emitters pass `level=` for
  the other case. `LEGACY_EVENT_KINDS` = `approval.reopened`, `tailor.done`, `tailor.failed`,
  `render.done`: only imported history holds them (§9.4, §9.6). `FOLLOW_UP_KINDS` and
  `FOLLOW_UP_EVENT` live in the catalogue too.
- **Run JSON.** `SearchInput.hints/brief/criteria` are open dicts: `SearchBrief` and
  `DerivedCriteria` belong to `matching.brief` / the prefilter, which read them leniently.
  `CompanyAddOutput.company` is an open dict (the companies page's `CompanyRow`, not yet
  written); `CompanyNamesOutput.added/notFound` are lists of names. `TailorOutput` and
  `RenderOutput` carry an optional `density` (§11.6). `SearchOutput.strong` defaults to 0 for
  imported runs. `ImportOutput.garble` is a `GarbleReport` model with today's keys.
- **Also in the catalogue:** `ACTIVE`/`TERMINAL` (plain strings), `RunStatus`, `Mode`,
  `ApprovalKind`, `ApprovalStatus`, `DecidedVia`, `NextStep`, `Level`, `PHASES`, `STATS_KEYS`,
  `PAYLOAD_MODELS`, `APPROVAL_NEXT_STEP`, `StoredDecision`. The model enums of the other apps
  live in their `models.py` (`Stage`, `SourceType`, `CompanyStatus`, `FetchStatus`,
  `DiscoveredVia`, `JobSource`, `Remote`, `Period`, `DocumentKind`, `ResumeKind`, `Role`,
  `Purpose`, `Label`, `ImportStatus`). `llm.models.Purpose` asserts at import that it lists
  `foray.env.PURPOSES` in order.

## 7. Shared modules: what else they hold

- `resumes/schema.py` also has `with_ids` and `fact_ids` (they live in `schema.ts` today, and
  lanes cannot edit this module), plus `TAILOR_SECTIONS`. Models carry no class docstrings,
  because pydantic would put them into the JSON schema sent to the model.
- `resumes/queries.py`: `latest_base_resume(user_id) -> BaseResume(id, version, resume) | None`
  (None when there is none or its data does not read, as `loadBaseResume` did) and
  `latest_base_resume_row(user_id) -> Resume | None`.
- `accounts/schemas.py` also has `SAMPLE_PREFERENCES` (every slot filled), for
  `create_test_user --with-resume` and the factories. `SLOTS` stays with step 3A
  (`accounts/profile.py`).
- `accounts/sample_resume.json` is synthetic: 3 roles, 2 schools, 14 skills, 1 project, 1 link,
  with fixed fact ids; `Resume.model_validate(data).dump()` reproduces it byte for byte.
- `tests/factories.py` adds `make_message`, `make_llm_call` (`user=None` is a system row),
  `sample_resume_data`, `sample_fast_eval`, `sample_deep_eval` and `SAMPLE_RESUME_PATH` to the ten
  factories of §12.1. `make_run` writes the row only (no job); `make_event` writes directly
  (`emit` is the app's writer).
