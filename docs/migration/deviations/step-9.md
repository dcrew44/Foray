# Step 9: deviations and clarifications

Later steps read this before the design. Each entry: what, why, evidence.

Step 9 owns `Dockerfile`, `docker-compose.yml` and `.dockerignore`. The old `worker` target (the
TS worker on the Node Playwright image) and the TS `migrate` command are gone. `backend` serves
`migrate`, `api` and `worker`, and `web` is the Next standalone stage with the `DJANGO_ORIGIN`
build argument (§13.2, §13.3).

## 1. `foray` is uid 1002, not 1001

- **What.** The backend stage runs `useradd --uid 1002 --create-home foray`.
- **Why.** In `mcr.microsoft.com/playwright/python:v1.63.0-noble`, uid 1000 is `ubuntu` and uid
  1001 is the image's own `pwuser`, so §13.2's `useradd -u 1001 foray` fails and the build stops.
  Nothing depends on the number: the compose stack uses named volumes only, and `/data` is
  `chown`ed to `foray` at build. The `web` image keeps uid 1001 (the Node image has it free).
- **Evidence.** `docker run --rm mcr.microsoft.com/playwright/python:v1.63.0-noble useradd -u 1001
  foray` → `useradd: UID 1001 is not unique`, exit 4. In the image, `getent passwd` lists
  `ubuntu:1000` and `pwuser:1001`.
- **`--create-home` (addition).** It gives Chromium and fontconfig a writable `~/.cache`; the
  worker's first render created `/home/foray/.cache`. It is not load-bearing: the same image,
  run as uid 65534 with `HOME=/nonexistent`, rendered a 1-page PDF too.

## 2. `STORAGE_DIR: /data` is set in the api and worker `environment`

- **What.** api and worker set `STORAGE_DIR: /data` next to §13.3's `DATABASE_URL` and
  `DJANGO_ORIGIN`.
- **Why.** Compose gives `env_file` precedence over the image's `ENV`, and the dev `.env` names a
  host path (the main checkout's `storage/`, step-0.md §7). Without the line, the containers would
  store files under a macOS path that does not exist in the container, which `foray` cannot
  create.
- **Evidence.** The merged config (`docker compose … config`, printed by key) gives
  `STORAGE_DIR=/data` for api and worker and the host path for migrate, which does not set it
  and never touches storage. The render below stored
  `/data/<user id>/resume_pdf/<ms>-Avery_Quinn_-_Resume.pdf`, owned by `foray`.

## 3. Additions the design leaves open

- **Bytecode at build.** `UV_COMPILE_BYTECODE=1`, and `python -m compileall -q foray apps` after
  the sync: `foray` cannot write `__pycache__` into the root-owned venv or app tree, so every
  process start would otherwise compile everything again. The build logged "Bytecode compiled
  7622 files".
- **A uv cache mount** (`--mount=type=cache,target=/root/.cache/uv`) on both `uv` steps. It is not
  part of the image. With `UV_LINK_MODE=copy`, the venv holds real files.
- **api healthcheck timing.** §13.3 gives only the test. Docker's default is one check every 30 s
  with no start interval, which would hold `web` back by at least 30 s. The stack uses
  `start_period: 60s`, `start_interval: 2s`, then `interval: 30s` (one access-log line per check).
  `up --wait` had every service ready 11 s after the start; the api log held 2 health lines in its
  first 50 s.
- **db.** `POSTGRES_DB: foray_app` as §13.3 says, and the healthcheck checks `-d foray_app` to
  match. The comment notes that this only matters for a fresh volume.
- **web.** The `deps` stage also copies `pnpm-workspace.yaml`, so the install sees the repo's
  `onlyBuiltDependencies`/`ignoredBuiltDependencies`. The image no longer creates `/app/data`:
  web stores nothing. As §13.3 says, web has no `env_file`.
- **`.dockerignore`.** The design's `.pytest_cache` is written `**/.pytest_cache` (pytest's cache
  sits in `backend/`), and `**/.ruff_cache` is added for the same reason.

## 4. The prodcheck override (not committed; the verifier recreates it)

The Done check ran with this file in the session's scratch directory, outside the repo (step 9
owns only the three files):

```yaml
# docker compose -p foray-prodcheck --profile prod -f docker-compose.yml -f prodcheck.override.yml …
services:
  db:
    ports: !reset []           # 5432 belongs to the dev foray-db-1
  migrate:
    environment: &check-env
      DEBUG: 'true'            # create_test_user, dev_session and /api/health/stream are DEBUG-only
      PUBLIC_ORIGIN: http://localhost:3100
      JSEARCH_ENABLED: 'false' # nothing in the check may spend
      LEGACY_DATABASE_URL: ''  # empty means unset (§1.3)
      LEGACY_STORAGE_DIR: ''
      BACKUP_DIR: /tmp/backups
  api:
    environment: *check-env
  worker:
    environment: *check-env
  web:
    ports: !override
      - '3100:3000'
```

`PUBLIC_ORIGIN` makes the Origin check accept `http://localhost:3100` for the unsafe requests the
check sends. The legacy and backup variables keep the dev `.env`'s host paths and the legacy URL
out of the containers. The merged config publishes only `3100→3000` (web), and its volumes are
`foray-prodcheck_db-data` and `foray-prodcheck_app-storage`.

## 5. Checks run in this step

All under `docker compose -p foray-prodcheck --profile prod -f docker-compose.yml -f <override>`,
from fresh volumes. The full build ran first, from the base images with no cache. The final run
used the committed files.

- **Images and migrate.** The build exits 0. `uv python install 3.14` installed CPython 3.14.6
  into `/opt/uv/python`. `next build` succeeded; its `BetterAuthError` lines come from the legacy
  `(app)` pages (step-8A.md §10). `up -d --wait` exit 0. migrate `Exited (0)`, 68 migrations `OK`.
- **User and Python.** In api, worker and a `run --rm --no-deps migrate` container, the user is
  `uid=1002(foray)`. `python` is `/app/backend/.venv/bin/python`, which resolves to
  `/opt/uv/python/cpython-3.14.6-linux-aarch64-gnu/bin/python3.14`, Python 3.14.6. PID 1 is
  uvicorn or `manage.py worker`, owned by `foray`. `/root` is `drwx------ root`, unreadable to
  `foray`; `/opt/uv/python` is `drwxr-xr-x`.
- **Auth through web.** `create_test_user --with-resume` and `dev_session` ran in the api
  container. `:3100/api/me` answered 401 `{"ok":false,"message":"Not signed in."}` without the
  cookie, and 200 with the user's `{id, name, email, signupOpen}` with it.
- **`/sign-in`.** Signed out: 200 `text/html`, title "Sign in – Foray", with "Welcome back" and
  "Create an account". The `_next/static` chunks it links answered 200. Signed in: 307 to `/home`,
  so the server component reached `api:8000` at runtime. The built routes manifest rewrites
  `/api/:path*` to `http://api:8000/api/:path*`.
- **Stream.** `curl -N --compressed :3100/api/health/stream`: ticks at +0.000 s, +0.985 s and
  +1.987 s. The headers were `text/event-stream`, `no-cache, no-transform` and
  `x-accel-buffering: no`, with no `content-encoding`.
- **Worker.** `:3100/api/live` read `worker.online: true`. `POST :3100/api/worker/ping`, sent with
  `Origin: http://localhost:3100`, answered `{"ok":true,"jobId":6}`. The `worker.ping` event
  `{jobId: 6, workerPid: 1}` arrived 0.14 s later. The api access log has no `/api/live` lines
  (§13.4).
- **PDF (the lane's extra check).** `POST :3100/api/resume/render` answered 202. The worker
  completed the run in 0.68 s: 1 page, density normal, "Rendered your resume to a 1-page PDF.".
  `GET :3100/api/documents/{id}` answered 200 `application/pdf`, 30,572 bytes starting `%PDF-1.4`,
  with Producer "Skia/PDF m153", `inline; filename="Avery Quinn - Resume.pdf"`, `private, no-store`
  and `nosniff`. The text is the sample resume, laid out on one page. Chromium ran as
  `/ms-playwright/chromium_headless_shell-1243/…/chrome-headless-shell`, as `foray`, driven by the
  venv's `playwright` 1.63.0.
- **Shutdown.** `docker compose stop worker` (SIGTERM to PID 1) took 0.46 s with a launched PDF
  browser: "stopping (running jobs get 60.0s)", then "stopped", exit 0. In the first run (same
  images; only the api healthcheck timing differed), `stop api` took 0.6 s: uvicorn logged
  "Shutting down" and "Finished server process [1]", exit 0.
- **Teardown.** `create_test_user --purge` → "Deleted 1 test user.". `down -v --rmi local` removed
  the five containers, the network, both `foray-prodcheck_*` volumes and the four built images.
  Afterwards the only container was `foray-db-1`, with the same `StartedAt`
  (2026-10-08T21:10:04Z), and the only volume was `foray_db-data`. Nothing listened on :3100.
- **Static.** `next typegen`, then `tsc --noEmit`, exit 0; `eslint`, exit 0. pytest
  (`FORAY_TEST_DB_NAME=test_foray_app_lane9 --create-db`): 1700 passed and 1 failed,
  `tests/accounts/test_auth.py::test_sliding_session_rules` (`assert 400 == 503`). That failure is
  the open handoff of step-6.md §4 and step-7.md §9, not this step's files.
- **Money and legacy.** No model was called ($0). JSearch was off in the containers. The legacy
  `foray` write counters stayed at 41158/1014040/5351.

## 6. Notes for later steps

- **The dev db container restarts once.** This step changes the `db` service's `POSTGRES_DB` and
  healthcheck, as §13.3 asks. The next `pnpm db:up` (project `foray`) therefore recreates
  `foray-db-1` on the same `foray_db-data` volume: the data stays, and the server restarts once.
  This lane never ran compose against the `foray` project.
- **`pnpm docker:prod` on the dev machine** runs the prod profile in project `foray`. That is the
  dev `foray-db-1` and its `foray_app`, and web on :3000, which conflicts with `pnpm dev`. Use a
  project name and an override like §4 to try the images beside the dev stack.
- **Legacy `(app)` pages in the image.** web has no `env_file`, so a page that still imports the
  legacy `env.ts` or Better Auth fails at request time in the container. Steps 8B–8D convert them,
  and step 10 checks them through the stack. `/sign-in`, `/` and the proxy redirects already work.
- **The prod `.env`.** The containers read the repo's `.env` through `env_file`. The dev file sets
  `DEBUG=true`, a host `BACKUP_DIR` and a host `LEGACY_*`. A deployment's `.env` should set
  `DEBUG=false`, a real `PUBLIC_ORIGIN` and `SECRET_KEY`, and no `LEGACY_*` (step 11 writes
  `.env.example`).
