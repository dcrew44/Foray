# Step 1: deviations and clarifications

Later steps read this before the design. Each entry: what, why, evidence.

## 1. `FORAY_TEST_DB_NAME` names the pytest database (addition)

- **What.** `foray/settings_test.py` sets `DATABASES["default"]["TEST"]["NAME"]` from
  `FORAY_TEST_DB_NAME`, default `test_foray_app`.
- **Why.** Parallel build lanes run pytest at the same time against the same Postgres server; each
  lane sets its own name.
- **Evidence.** `FORAY_TEST_DB_NAME=test_foray_app_step1_lane uv run pytest --create-db` built and
  used `test_foray_app_step1_lane` (39 passed), which was then dropped.

## 2. `"ninja"` is an installed app

- **What.** `INSTALLED_APPS` gains `"ninja"` after `procrastinate.contrib.django`.
- **Why.** `manage.py export_openapi_schema` (used by `pnpm api:types`, §1.4, §5.4) is a command of
  the `ninja` Django app; Django only finds it when the app is installed.
- **Evidence.** Without it: `CommandError: Unknown command: 'export_openapi_schema'`. With it the
  export works and honours the registry merge in `ForayAPI.get_openapi_schema` (§15.4 assumption
  confirmed: `test_wire_format.py::test_export_includes_registered_schemas`).

## 3. `CommonMiddleware` enforces `ALLOWED_HOSTS`

- **What.** `MIDDLEWARE` gains `django.middleware.common.CommonMiddleware` right after
  `SecurityMiddleware`.
- **Why.** Django validates `Host` only when something calls `request.get_host()`. Nothing in the
  design's middleware list does, so `ALLOWED_HOSTS` was not enforced at all. With
  `APPEND_SLASH=False` the middleware does nothing else that matters here (it never touches
  streaming responses).
- **Evidence.** Before: `curl -H 'Host: evil.example' 127.0.0.1:8000/api/health` → 200. After:
  `api`, `localhost`, `127.0.0.1` → 200; `evil.example` → 400 (`DisallowedHost`).

## 4. `dev:api` and `dev:worker` run `python -m foray.env` first

- **What.** Both scripts are `cd backend && uv run python -m foray.env && uv run …` (the rest is
  §1.4 verbatim). `python -m foray.env` runs the static boot checks only: no Django, no connection.
- **Why.** Under `--reload`, uvicorn's supervisor never imports the app; the child that does exits 1
  on a configuration error and the supervisor keeps running (watchfiles behaves the same). So
  `DATABASE_URL= pnpm dev:api` printed the error but never exited.
- **Evidence.** Before: the process stayed up until killed (exit 143). After: `DATABASE_URL=`,
  `DEBUG=false LLM_FAKE=1` and `DATABASE_URL=postgres://foray:foray@127.0.0.1:1/foray` each exit 1
  with `Configuration error:` naming the variable (the last one without connecting: nothing
  listens on port 1).

## 5. JSON is rendered compactly

- **What.** `foray.api.CompactJSONRenderer` dumps with `separators=(",", ":")` and
  `ensure_ascii=False`; ninja's default prints `{"ok": true}`.
- **Why.** The Done check expects `{"ok":true}` literally, and it matches `JSON.stringify`.
- **Evidence.** `curl -s localhost:3000/api/health` → `{"ok":true}`.

## 6. `UniqueConstraint(fields=[...])`, not positional strings

- **What.** `RateLimit` uses `UniqueConstraint(fields=["key", "window_start"], name=…)`.
- **Why.** In Django 6.1 positional arguments of `UniqueConstraint` are *expressions*; the design's
  shorthand `UniqueConstraint("key", "window_start", …)` generates `F("key"), F("window_start")`, a
  functional unique index rather than a column constraint, which `ON CONFLICT (key, window_start)`
  should not rely on. **Step 2: write every unique constraint of §2.2 with `fields=`.**
- **Evidence.** The first `makemigrations` emitted
  `models.UniqueConstraint(models.F('key'), models.F('window_start'), …)`; the committed migration
  has `fields=('key', 'window_start')`.

## 7. How routers are mounted

- **What.** Each app's `api.py` exposes `ROUTERS: dict[prefix, Router]` (one router per §5.1
  prefix, built by `core.router.router()`), and `foray/api.py` mounts every module's `ROUTERS`.
  Later steps add operations to those routers (e.g. `from apps.accounts.api import auth`) and never
  edit `foray/api.py`. The API's own default router stays empty; `test_wire_format.py` fails if an
  operation lands on it or if any router is not `by_alias=True`.
- **Why.** §5.1 fixes the prefixes but not the mechanism.

## 8. The validation-message mechanism (§5.2)

- **What.** An operation names its sentences with `@messages(invalid=…, not_found=…)` from
  `apps.core.errors`, placed **under** the router decorator. `invalid` may be a callable
  `(errors) -> str` (for `PUT /api/preferences` and `PATCH /api/matches/{id}`). A body or query
  error answers 400 with `invalid`; a path-parameter error (a malformed id) or `Http404` answers
  404 with `not_found`. Defaults: "That request could not be read." / "That no longer exists.".
  `ApiError(status, message, **extra)` answers `{ok:false, message, **extra}`; not signed in is
  401 "Not signed in."; anything else is 500 with the generic sentence, logged.
- **Why.** ninja's exception handlers do not receive the operation, so `errors.install(api)` adds an
  API-level ninja "view" decorator that records the running operation on the request.
- **Evidence.** `test_wire_format.py::test_errors_use_the_envelope`.

## 9. `Cache-Control: no-store` is set centrally

- **What.** `ForayAPI.create_response` adds `Cache-Control: no-store` to every JSON response to a
  GET that has no `Cache-Control` yet (§5.2). File responses set their own headers.

## 10. pyright: one rule off

- **What.** `reportIncompatibleVariableOverride = false` in `[tool.pyright]`; standard mode
  otherwise, with `django-types` (§15.4 assumption: usable, confirmed: 0 errors).
- **Why.** django-types flags every Django declarative override (`class Meta` under
  `Timestamped`, `objects`, `REQUIRED_FIELDS`, `User.password`). Step 2's
  `class Meta(UserOwned.Meta)` models would all hit it.

## 11. ruff skips migrations

- **What.** `extend-exclude = ["apps/*/migrations"]`, so `makemigrations` output never fails
  `ruff format --check`.

## 12. Register strictness

- **What.** `tests/test_register.py` reports a row whose test *file* has not landed as a skip
  ("R5 pending: tests/runs/test_live_cursor.py"); a file that exists without the named test fails.
  `FORAY_REGISTER_STRICT=1` turns pending rows into failures. **Step 10 onwards runs pytest with
  `FORAY_REGISTER_STRICT=1`** (§0: "from step 10 on, a missing test fails"). Step 11 changes
  `REGISTER_SOURCE` to DESIGN.md.
- **Also.** The §1.5 greps ignore comments and string literals (tokenizer-based), so docstrings may
  mention `auto_now` or `.defer(`. A `get_object_or_404(` is flagged when its first argument names
  a model with a `_plain` manager (a `UserOwned` model). A chat tool is any function in
  `apps/chat/tools.py` with a decorator whose name contains "tool".

## 13. Test scaffolding details

- `tests/conftest.py` empties procrastinate's tables before and after every
  `django_db(transaction=True)` test: its models are unmanaged, so Django's flush between
  transactional tests leaves committed jobs behind.
- `tests/queue.py`: `deferred(task_name)` returns `DeferredJob(id, task_name, queue, args, lock,
  queueing_lock)`; `run_deferred(queues)` (sync) and `await arun_deferred(queues)` (async);
  `worker_connector()`; `clear_queue()`. Raw cursors return `jsonb` as text under Django's psycopg
  setup, so `deferred()` decodes `args`. §15.4 assumptions confirmed: `defer_in_tx` joins the
  caller's transaction under the worker connector, and `run_worker_async(wait=False)` drains the
  queue and returns (`tests/core/test_queue.py`).
- `tests/fakes.py`: `fake_models(monkeypatch, {purpose: model})` resolves each faked purpose to the
  ref `fake:<purpose>` and patches `registry.model_ref`/`registry.base_model`, and gives
  `registry._MODELS` (the per-process cache) an empty dict for the test. **Step 3B keeps those
  three names**: `model()` must look up `model_ref` and `base_model` as module globals at call time
  and cache in `_MODELS`, and `pricing` treats `fake:*` as free (§8.9).
- The suite runs with `--reuse-db --import-mode=importlib`, so `tests/<app>/` directories need no
  `__init__.py`, and two lanes may both have, say, `test_pipeline.py`.

## 14. Smaller clarifications

- `foray/asgi.py` runs `assert_app_database` in a helper thread: uvicorn imports the app inside its
  event loop, where Django raises `SynchronousOnlyOperation` for sync database access. Verified:
  importing `foray.asgi` inside `asyncio.run` against a database with a `pgboss` schema raises
  `ImproperlyConfigured` and writes nothing.
- The `/api/live` access-log filter is attached to `uvicorn.access` in `foray/asgi.py`, not via
  `LOGGING` (a dictConfig entry for that logger would replace uvicorn's own handler).
- `/api/health/stream` is `include_in_schema=False`, so `openapi.json` does not depend on `DEBUG`.
- `apps.llm.structured.structured`'s `timeout` parameter carries `# noqa: ASYNC109` (the §8.4
  signature is final; the body wraps `asyncio.timeout`).
- `test_guards.py` creates and drops scratch databases named `foray_guard_<hex>`. The
  `reset_app_db` allowlist case of §12.3 belongs with that command (step 3F).
- The Done check's `ts -s` was replaced by a Python line timestamper (moreutils is not installed).
- `tsc --noEmit` needs Next's generated route types (`LayoutProps`): run `next typegen` (or
  `pnpm dev` once) in a fresh worktree first. This predates step 1.
