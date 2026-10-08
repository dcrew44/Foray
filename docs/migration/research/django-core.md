# Django core for the Foray server migration (topic: django-core)

Researched 2026-10-04 on macOS arm64. Everything under "Executed" was run in a scratch venv
(`research/django-core/venv`, uv-managed CPython 3.14.5) against a throwaway
`pgvector/pgvector:pg18` container (Postgres 18.6, port 55432, tmpfs, now stopped) whose
schema was built by piping the repo's own `drizzle/000*.sql` files into `psql`. The repo and
its `node_modules` were only read (`git status` is clean; `find -newer` on the next/react
store dirs found nothing written). The scratch Django project is at
`research/django-core/proj` (and `proj_fake` for the migration-ownership experiment).

Legend: **[ran]** = I executed it and the output is quoted; **[src]** = read in installed
package source; **[doc]** = docs/release notes only.

---

## 1. Recommendation (short)

| Concern | Choice |
|---|---|
| Django | **6.1.1** now (released Aug 2026; supports Python 3.12–3.14). Plan the hop to **6.2 LTS (April 2027)**. |
| Python | 3.14 (`requires-python = ">=3.14"`, `.python-version` = 3.14) |
| API framework | **django-ninja 1.7.1** (pydantic 2.13, async views + async auth, OpenAPI 3.1, built-in `SSE[...]`/`JSONL[...]` streaming). Plain async Django views for the chat stream. Not DRF. |
| ASGI server | **uvicorn[standard] 0.54** (uvloop + httptools), always with `--timeout-graceful-shutdown 5`; dev with `--reload`. Granian 2.8.4 is an acceptable alternative (`--interface asginl`, `--workers-kill-timeout`). Not daphne. Never `manage.py runserver` for streaming. |
| DB driver/pool | **psycopg 3.3.6 + psycopg-pool 3.3.3** via Django's built-in `OPTIONS["pool"]`, `CONN_MAX_AGE = 0`, `server_side_binding` off (default). |
| User model | `accounts.User(AbstractBaseUser)` on `db_table="user"`, `TextField` pk, `password = None`, `last_login = None`, UTC-naive timestamp field for Better Auth's `timestamp` columns. |
| Schema ownership | Drizzle stays owner while Next still writes those tables; Django models `managed=False`. Flip to Django-owned migrations in one deliberate commit, applied with `migrate --fake-initial`, and verified by a `pg_dump --schema-only` diff (fake-initial checks table names only). |
| Auth during transition | Django reads Better Auth's signed `better-auth.session_token` cookie and looks up `session` (verified byte-compatible). Later: `django.contrib.sessions` DB backend (not signed cookies). |
| CSRF | Same-origin through Next: `CSRF_TRUSTED_ORIGINS=[APP_ORIGIN]` (or `USE_X_FORWARDED_HOST=True`), ninja `APIKeyCookie(csrf=True)` on unsafe methods, frontend sends `X-CSRFToken`. |
| Next routing | `rewrites().fallback: [{ source: "/api/:path*", destination: DJANGO + "/api/:path*" }]` — deleting a Next `route.ts` hands that path to Django. Every streaming response must send `Cache-Control: no-cache, no-transform`. |
| Layout | top-level `backend/` uv project: `backend/pyproject.toml`, `backend/foray/` (settings, asgi, urls, api), apps under `backend/apps/*`. One `settings.py` + typed env loader. |

---

## 2. Versions (installed and exercised)

From `uv pip install` into the scratch venv and PyPI JSON (`upload_time` of the latest file):

```
Django                 6.1.1      uploaded 2026-09-02  requires_python=>=3.12
django-ninja           1.7.1      uploaded 2026-09-19
djangorestframework    3.18.1     uploaded 2026-09-07
uvicorn                0.54.0     uploaded 2026-09-25   (+ uvloop 0.23.0, httptools 0.8.0, watchfiles 1.3.0)
granian                2.8.4      uploaded 2026-09-30
daphne                 4.2.3      uploaded 2026-07-21
psycopg                3.3.6      uploaded 2026-09-18   (psycopg-binary 3.3.6)
psycopg-pool           3.3.3      uploaded 2026-09-22
pydantic               2.13.5     (pydantic-core 2.46.5)
asgiref                3.12.1
httpx2                 2.13.1     (used as the async test client)
dj-database-url        3.1.2
pydantic-settings      2.15.0
pytest-django          4.14.0     (resolved by uv add in the layout prototype)
next                   16.3.5     (repo's node_modules, used read-only via symlinks)
better-auth            1.7.5      (repo's node_modules, read-only)
```

Django support table [doc, djangoproject.com/download]: 6.1 mainstream until April 2027,
extended until Dec 2027; 5.2 LTS extended until April 2028; 6.2 (April 2027) is the last
release under the old policy and is LTS; after that calendar versioning ("Django 2028").

Python 3.14 [ran]: Django's wheel metadata lists `Programming Language :: Python :: 3.14`,
`Requires-Python: >=3.12`; every experiment below ran on 3.14. Note uv chose its managed
CPython **3.14.5**, not Homebrew's 3.14.7 — pin with `uv python pin 3.14` and
`uv python install 3.14` (or `UV_PYTHON_PREFERENCE=only-system`) if the patch level matters.

---

## 3. Django 6.0/6.1 changes that matter here [doc]

- 6.1: **model field fetch modes** (`FETCH_ONE`, `FETCH_PEERS`, `FETCH_RAISE`); **DB-level
  `on_delete`** (`DB_CASCADE`, `DB_SET_NULL`, `DB_SET_DEFAULT`) — `inspectdb` now emits these
  from the live FKs [ran]; `UUID4()`/`UUID7()` DB functions; `GeneratedField` virtual
  columns on PG18; PG14 support dropped (PG15+); signed cookies switched to an unambiguous
  salt (`SIGNED_COOKIE_LEGACY_SALT_FALLBACK` now False); `MAILERS` setting (startproject
  already writes it [ran]); deprecation classes renamed to calendar versions
  (`RemovedInDjango2028Warning`).
- 6.0: built-in **Tasks framework** (`django.tasks`, `@task`, `TASKS` setting) — ships only
  dev/test backends and **no worker**; `DEFAULT_AUTO_FIELD` defaults to `BigAutoField`;
  built-in CSP middleware; `AsyncPaginator`/`AsyncPage`; Python 3.12+ only.
- No async DB backend yet [src]: `grep "async def" django/db/backends/` → 0 hits. The async
  ORM is still `sync_to_async` wrappers (see §5).

---

## 4. ASGI streaming (SSE) — the core experiment

### 4.1 The view [ran]

```python
# api/sse.py (scratch project)
async def async_sse(request):
    count = int(request.GET.get("count", 10)); interval = float(request.GET.get("interval", 0.5))
    tag = request.GET.get("tag", "async")
    async def gen():
        n = 0
        try:
            for n in range(count):
                yield f"data: {json.dumps({'n': n, 't': round(time.time(), 3), 'pid': os.getpid()})}\n\n"
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            log.info("[%s] CancelledError at n=%d (client went away)", tag, n); raise
        finally:
            log.info("[%s] finally: cleanup ran at n=%d", tag, n)
    return StreamingHttpResponse(gen(), content_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})
```

Client timestamping: `curl -N -s URL | python3 -u stamp.py` (stamp.py prints `+seconds` at
each line's arrival).

### 4.2 Results: unbuffered on all three servers [ran]

```
== port 8701 (uvicorn 0.54)          == port 8702 (granian 2.8.4)        == port 8703 (daphne 4.2.3)
+ 0.000s  data: {"n": 0, ...}        + 0.000s  data: {"n": 0, ...}       + 0.000s  data: {"n": 0, ...}
+ 0.484s  data: {"n": 1, ...}        + 0.490s  data: {"n": 1, ...}       + 0.490s  data: {"n": 1, ...}
+ 0.984s  data: {"n": 2, ...}        + 0.991s  data: {"n": 2, ...}       + 0.990s  data: {"n": 2, ...}
+ 1.486s  data: {"n": 3, ...}        + 1.492s  data: {"n": 3, ...}       + 1.491s  data: {"n": 3, ...}
+ 1.987s  data: {"n": 4, ...}        + 1.993s  data: {"n": 4, ...}       + 1.992s  data: {"n": 4, ...}
```

uvicorn with `--loop uvloop --http httptools` on 3.14: same 0.5 s cadence [ran].

### 4.3 Things that DO buffer [ran]

- **A sync generator under ASGI** is fully buffered: Django logs
  `StreamingHttpResponse must consume synchronous iterators in order to serve them
  asynchronously` and does `await sync_to_async(list)(...)` [src: `django/http/response.py`
  `__aiter__`]. All 4 chunks arrived at `+2.002s`.
- **`manage.py runserver` (WSGI)** buffers an async generator (`async_to_sync(to_list)`):
  all 4 chunks at `+1.996s`. Dev must run uvicorn, not runserver.
- **Next 16 rewrite + gzip** (see §4.6): buffered unless the response has `no-transform`.

### 4.4 Client disconnect stops the generator [ran]

Django 6.1 runs `listen_for_disconnect(receive)` and the response sender in one
`asyncio.TaskGroup`; `http.disconnect` raises `RequestAborted`, which cancels the
sender, and the async generator sees `CancelledError` [src: `django/core/handlers/asgi.py`
`handle()`]. `curl -m 1.3` against a 20-tick stream:

```
--- uvicorn  [disc8701] CancelledError at n=2 (client went away) / finally: cleanup ran at n=2
--- granian  [disc8702] CancelledError at n=2 (client went away) / finally: cleanup ran at n=2
--- daphne   [disc8703] CancelledError at n=2 (client went away) / finally: cleanup ran at n=2
```

Disconnect while the generator is blocked in a 10 s `await` (like waiting on an LLM token):
curl exited at 13:50:00.337, uvicorn logged `CancelledError at n=0` at 20:50:00.306 (local vs
UTC clock) — i.e. within tens of ms, not at the next write. Same for granian and daphne.
Through the Next rewrite the cancellation also propagates (`[nextdisc] CancelledError at n=2`).

### 4.5 Shutdown with an open stream [ran]

```
uvicorn-default: SIGTERM with 1 open SSE -> STILL-RUNNING after 26.05s
uvicorn --timeout-graceful-shutdown 3: SIGTERM with 1 open SSE -> exited after 3.34s
granian asginl default: SIGTERM with 1 open SSE -> STILL-RUNNING after 25.78s
```

So: always pass `--timeout-graceful-shutdown` (uvicorn) / `--workers-kill-timeout` (granian),
or a deploy/`--reload` hangs on any open chat stream. A forced stop does not run the
generator's cleanup, so persist chat output so a killed stream loses little (the TS route
persists in `onEnd`; the Python port should do the same and accept that a killed stream is lost,
or persist incrementally).

Other server notes [ran]: granian with `--interface asgi` logs
`ASGI Lifespan errored, continuing without Lifespan support (to avoid Lifespan completely use
"asginl" interface)` — Django has no lifespan; use `asginl`. uvicorn logs
`ASGI 'lifespan' protocol appears unsupported` (harmless). Django's `LOGGING` with the default
`disable_existing_loggers=True` silences uvicorn's own loggers and Django's error logging —
set `"disable_existing_loggers": False` (this hid a real error from me until I did).

Concurrency [ran]: 40 concurrent async requests each doing `pg_sleep(0.2)` with pool
`max_size=4`: uvicorn 2.09–2.12 s, granian 2.10–2.11 s (ideal 2.0 s) — both pool-bound, no
server difference worth choosing on.

Why uvicorn over granian: equal on streaming and disconnects in these tests; uvicorn has the
simpler Django story (lifespan noise only, built-in `--reload` via watchfiles,
`--proxy-headers`, the most deployment docs), and the shutdown flag is one obvious option.
Granian is faster on raw throughput (not measured here; not this app's bottleneck) and fine if
preferred. Daphne (Twisted) only makes sense with Channels/WebSockets, which Foray does not
need.

### 4.6 Through a Next 16.3.5 rewrite [ran]

Scratch Next app (`research/django-core/nextproxy`, symlinking the repo's `next`, `react`,
`react-dom`; `next dev --webpack`). Findings:

1. **Headers Django receives** (`/csrf/allheaders`):
   ```json
   {"Cookie": "a=b", "Host": "127.0.0.1:8701", "User-Agent": "curl/8.7.1", "Accept": "*/*",
    "X-Forwarded-Proto": "https", "X-Forwarded-Host": "127.0.0.1:3999", "Connection": "keep-alive"}
   ```
   Host is rewritten to the Django upstream; `X-Forwarded-Host` carries the browser's host;
   `Origin` and cookies pass through. `X-Forwarded-Proto` above is the value *curl sent* —
   Next forwarded a client-supplied header and did not add X-Forwarded-For itself, so only
   trust XFP when an edge proxy overwrites it.
2. **Compression buffers SSE.** Next's router server wraps every response, rewrites included,
   in the `compression` middleware unless `Cache-Control` contains `no-transform`
   [src: `next/dist/server/lib/router-server.js` lines ~124 and ~257; compiled `compression`
   `shouldTransform`]. django-ninja's built-in `SSE` sets only `Cache-Control: no-cache`, so:
   ```
   == ninja SSE[Tick] via Next, Accept-Encoding: gzip
   cache-control: no-cache
   Content-Encoding: gzip
   + 0.000s  10 bytes
   + 1.995s  89 bytes            <- the whole stream at the end
   == plain view with "no-cache, no-transform", Accept-Encoding gzip
   + 0.000s  51 bytes / + 0.490s 50 bytes / + 0.991s 51 bytes / + 1.492s 51 bytes
   ```
   Browsers always send `Accept-Encoding: gzip`. **Every streaming response from Django must
   carry `Cache-Control: no-cache, no-transform`** (a small middleware that sets it for
   `text/event-stream`, or set `compress: false` in `next.config.ts` and compress at the edge).
3. **POST chat-style stream** (AI SDK UI message stream shape, `x-vercel-ai-ui-message-stream:
   v1`, `[DONE]`) through the fallback rewrite with `Accept-Encoding: gzip, deflate, br, zstd`:
   deltas at +0.000/+0.393/+0.794/+1.195 s — unbuffered, custom headers
   (`x-conversation-id`) preserved.
4. **Strangler routing via `fallback` rewrites**:
   ```js
   rewrites: async () => ({ fallback: [{ source: "/api/:path*", destination: `${DJANGO}/api/:path*` }] })
   ```
   ```
   /api/runs/abc/events  -> {"servedBy":"next"}    (a Next route.ts exists, it wins)
   /api/live             -> {"servedBy": "django"} (no Next handler -> Django)
   /api/py/health        -> Django
   /api/nonexistent      -> Django's 404
   ```
   Per the bundled docs (`node_modules/next/dist/docs/.../rewrites.md`), fallback rewrites run
   after filesystem and dynamic routes, so porting a slice = delete `src/app/api/<x>/route.ts`
   and the same URL lands on Django. Keep Django's URLs identical to today's (`/api/live`,
   `/api/runs/<id>/events`, …) and without trailing slashes (`APPEND_SLASH=False`).
   Caveat [doc, not executed]: `next.config` is evaluated at build time for
   `output: "standalone"`, so the Django URL should be a build arg or a stable in-network
   name like `http://api:8000`.

---

## 5. Async ORM maturity (Django 6.1.1) [ran]

`async_orm.py` output against the Drizzle-made schema:

```
aget: ada@example.com
acount: 2
async for: ['run.started', 'run.completed']
aiterator: [1, 2]
aaggregate: {'m': 2}
acreate Run with db_default id -> 1d085128-72da-4615-a305-84c3db328e95 status queued
abulk_create ids: [3, 4, 5]
aupdate: 3
aget_or_create created: True
lazy FK in async -> SynchronousOnlyOperation
select_related FK ok: search
FETCH_PEERS lazy FK in async -> SynchronousOnlyOperation
async with transaction.atomic() -> TypeError 'django.db.transaction.Atomic' object does not support the asynchronous context
with transaction.atomic() in async -> SynchronousOnlyOperation You cannot call this from an async context - use a thread or sync_to_async.
select_for_update async -> TransactionManagementError select_for_update cannot be used outside of a transaction.
sync_to_async(atomic fn) with select_for_update: running
adelete: (1, {'api.Run': 1})
has atomic async? False
```

Available async APIs [src]: QuerySet `aiterator aaggregate acount aget acreate abulk_create
abulk_update aget_or_create aupdate_or_create aearliest alatest afirst alast ain_bulk adelete
aupdate aexists acontains aexplain`, `aprefetch_related_objects`, related managers
`aadd aremove aclear aset acreate aget_or_create aupdate_or_create`, model `asave adelete
arefresh_from_db`; auth `alogin alogout aauthenticate aget_user`, `request.auser()`;
session stores have async methods.

Verdict: reads, writes and bulk ops are fine from async views. **No async transactions,
no lazy relation loading** (use `select_related`/`prefetch_related`; 6.1's `FETCH_PEERS`
does not help in async code). Anything transactional — approval decisions + continuation
enqueue, `select_for_update(skip_locked=True)` claims — goes in a plain sync function wrapped
with `sync_to_async` (works, shown above). Under ASGI each request gets its own
thread-sensitive executor (`ThreadSensitiveContext` in `ASGIHandler.__call__` [src]), so
async ORM calls of one request run serially on one thread holding one connection.

Postgres enums (`run_status`, `event_level`, …): plain `TextField`/`TextChoices` reads and
writes work with both client-side (default) and server-side binding [ran]
(`server_side_binding=False/True: enum insert+filter OK`).

---

## 6. psycopg 3 + Django's built-in pool

```python
DATABASES = {"default": {
    "ENGINE": "django.db.backends.postgresql", ...,
    "CONN_MAX_AGE": 0,                       # required: pool + persistent conns -> ImproperlyConfigured
    "CONN_HEALTH_CHECKS": False,             # True => pool check=ConnectionPool.check_connection
    "OPTIONS": {"pool": {"min_size": 2, "max_size": 10, "timeout": 10}},
}}
```

[src `postgresql/base.py`] `pool: True` means psycopg_pool defaults (min_size 4, max = min);
the pool is per process and created lazily; `CONN_MAX_AGE != 0` raises `"Pooling doesn't
support persistent connections."` `dj_database_url.parse(DATABASE_URL, conn_max_age=0)` then
`cfg["OPTIONS"]["pool"] = {...}` works with the repo's `postgres://` URL [ran].

Behaviour observed [ran] (pool max 4, `/api/py/pool` returns `pool.get_stats()`):

1. Short requests return their connection at `request_finished`: 20 concurrent requests,
   4 distinct backend pids, pool back to `available 4`.
2. **A stream that touched the DB holds its connection for the whole stream**:
   `while held stream is idling: size 2 avail 1` → `after it ends: avail 2`. N concurrent chat
   streams need N pooled connections unless the view releases it.
3. **Pool starvation is real**: 4 open polling SSE streams + 1 normal request →
   `500 in 5.02s` (PoolTimeout after `timeout=5`).
4. **`sync_to_async(connection.close)` is a bug**: the `connection` proxy is resolved on the
   event-loop thread, so the bound method belongs to a different `DatabaseWrapper`:
   ```
   django.db.utils.DatabaseError: DatabaseWrapper objects created in a thread can only be used
   in that same thread. The object with alias 'default' was created in thread id 8577384320 and
   this is thread id 6179287040.
   ```
   Resolve inside the sync function instead:
   ```python
   def _release():
       from django.db import connections
       connections["default"].close()        # returns it to the pool
   await sync_to_async(_release)()
   ```
   Probe log with the inner form: `n=0 after query ... has_conn=True` → `after close(inner)
   ... has_conn=False`, no leak.
5. **An exception raised inside an async streaming generator leaks the pooled connection
   permanently** (Django 6.1.1): the exception escapes `send_response`, `response.close()` is
   skipped, `request_finished` never fires, `close_old_connections` never runs.
   ```
   after boom (no guard) #1: size 2 avail 1
   after boom (no guard) #2: size 2 avail 0      <- two connections gone for good
   after boom (guard) #1:    size 3 avail 1
   after boom (guard) #2:    size 3 avail 1      <- try/finally release: no leak
   ```
   Related fixed ticket: #35059 / dup #35148 (request_finished skipped on disconnect, 5.0).
   I did not find a ticket for the exception-mid-stream case; worth filing. Mitigation:
   every streaming generator body is `try: ... except Exception: yield error-event; finally:
   await sync_to_async(_release)()` — which is the behaviour a chat stream wants anyway.

Design consequences: do DB work at the start (load history) and end (save) of a chat stream
and release the connection in between; keep `/api/live` as a short polled JSON endpoint (as
today) rather than a DB-polling SSE stream; size `max_size` per process with the TS worker
(pg `max=5/10`), Next/Better Auth pool and the Python worker in mind (PG default
`max_connections=100`).

---

## 7. Custom user model on Better Auth's existing `"user"` table

### 7.1 What `inspectdb` produced [ran] (abridged)

```python
class User(models.Model):
    id = models.TextField(primary_key=True)
    name = models.TextField(); email = models.TextField(unique=True)
    email_verified = models.BooleanField(); image = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(); updated_at = models.DateTimeField()
    class Meta: managed = False; db_table = 'user'

class Session(models.Model):
    ...; user = models.ForeignKey('User', models.DB_CASCADE)      # 6.1 emits DB_CASCADE/DB_SET_NULL
class Companies(models.Model):
    careers_urls = models.TextField()  # This field type is a guess.   <- really text[]
    source_type = models.TextField()  # This field type is a guess.    <- PG enum
    class Meta: unique_together = (('source_type', 'ats_slug'),)       <- WRONG: it is a partial
                                                                          unique index WHERE ats_slug IS NOT NULL
class Jobs(models.Model):
    salary_min = models.DecimalField(blank=True, null=True)            <- invalid (no max_digits)
```

inspectdb is a starting point only. Hand-fix: `ArrayField(TextField())` for `text[]`
(needs `django.contrib.postgres`), `TextChoices` for enums, `db_default=Now()` /
`db_default=Func(function="gen_random_uuid")` (inspectdb emits no DB defaults; without them
Django inserts fail NOT NULL), `auto_now=True` for `updated_at` (Drizzle's `$onUpdate` is
client-side), `Meta.indexes`/`UniqueConstraint(condition=...)` for the partial index, and the
Better Auth `timestamp` columns (next).

### 7.2 Timestamp gotcha [ran]

Only `user`, `session`, `account`, `verification` use `timestamp without time zone`; the
app tables use `timestamptz`. With `USE_TZ=True` Django returns naive values for `timestamp`:

```
plain DateTimeField on timestamp column -> datetime.datetime(2026, 10, 4, 20, 48, 42, 908820) tzinfo: None
compare with timezone.now() fails: can't compare offset-naive and offset-aware datetimes
UTCNaiveDateTimeField -> datetime.datetime(2026, 10, 4, 20, 48, 42, 908820, tzinfo=datetime.timezone.utc)
session filter expires_at__gt=now(): 1
session TimeZone: ('UTC',)
```

Drizzle writes these as UTC wall-clock, and Django pins the connection to UTC, so a field that
tags/strips UTC is correct:

```python
class UTCNaiveDateTimeField(models.DateTimeField):
    def db_type(self, connection):
        return "timestamp"  # without time zone, as Better Auth created it
    def from_db_value(self, value, expression, connection):
        return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value
    def get_db_prep_value(self, value, connection, prepared=False):
        value = super().get_db_prep_value(value, connection, prepared)
        if isinstance(value, datetime) and value.tzinfo is not None:
            value = value.astimezone(UTC).replace(tzinfo=None)
        return value
```

(Before I added `db_type`, the would-be DDL said `timestamp with time zone` and
`makemigrations` still printed "No changes detected" after I added it — the autodetector does
not see `db_type` changes. Alternative: migrate those four columns to `timestamptz` and set
`withTimezone: true` in Drizzle; Better Auth is fine with either.)

### 7.3 The model [ran]

```python
class User(AbstractBaseUser):
    id = models.TextField(primary_key=True, default=better_auth_id)   # 32 x [A-Za-z0-9]
    name = models.TextField()
    email = models.TextField(unique=True)
    email_verified = models.BooleanField(default=False, db_default=False)
    image = models.TextField(null=True, blank=True)
    created_at = UTCNaiveDateTimeField(db_default=Now())
    updated_at = UTCNaiveDateTimeField(db_default=Now(), auto_now=True)
    password = None          # hashes live in account.password (Better Auth "credential" rows)
    last_login = None        # django.contrib.auth then skips the update_last_login receiver
    USERNAME_FIELD = "email"; EMAIL_FIELD = "email"; REQUIRED_FIELDS = ["name"]
    objects = UserManager()
    class Meta:
        db_table = "user"
        managed = False
    def get_session_auth_hash(self):
        # must be non-empty and must not query (aget_user calls it in async code)
        return salted_hmac("foray.session-auth", self.pk, algorithm="sha256").hexdigest()
```

- `AUTH_USER_MODEL = "accounts.User"` must exist before the first `migrate` (auth's
  migrations depend on it); `manage.py check` passes [ran].
- Created rows match Better Auth's shape: `new user id: IZ8kb15MNpsBBAoXURyHAuKGohSahyYa 32`;
  real Better Auth ids look the same (`OWGqxny8cKfaXrs8naowFM3sSiAjbjtc`, 32) [ran
  `@better-auth/core/utils/id`].
- **`get_session_auth_hash` returning `""` silently logs everyone out**: `aget_user` →
  `AnonymousUser` because the stored hash is falsy. With the HMAC above:
  `aget_user from a fresh request: User U1aaaa...` [ran]. Drawback: a password change does not
  invalidate other sessions; to get that, derive it from a column stored on the user row (not
  a query to `account`).
- No `PermissionsMixin` (would need `is_superuser` plus M2M tables); add later if Django admin is wanted.
- Django's own tables (`django_session`, `django_migrations`, `auth_group`,
  `auth_permission`, `django_content_type`) land in `public` next to Drizzle's. `drizzle-kit
  generate` diffs its snapshot, not the DB, so it ignores them; if anyone ever uses
  `drizzle-kit push`, add `tablesFilter: ["!django_*", "!auth_*"]`.

### 7.4 Sharing Better Auth's login during the transition [ran]

Cookie = `encodeURIComponent(token + "." + base64(HMAC_SHA256(secret, token)))`
[src: `better-call@1.4.0/dist/crypto.mjs` `signCookieValue`, `context.mjs`
`getSignedCookie` — signature must be 44 chars ending in `=`]. Better Auth cookies are
`SameSite=Lax` [src]. Python verification:

```python
def unsign(raw):
    token, dot, sig = unquote(raw).rpartition(".")
    if not dot or len(sig) != 44 or not sig.endswith("="): return None
    good = base64.b64encode(hmac.new(SECRET.encode(), token.encode(), hashlib.sha256).digest()).decode()
    return token if hmac.compare_digest(good, sig) else None
```

A cookie signed by Better Auth's own `signCookieValue` in node
(`AbCdEf...012345.wFZ8n4D1n4DPWBUa3UFqBQUzKScc%2F%2BWiFp1OQRbkVOo%3D`) authenticated against
the ninja endpoint; no cookie or a forged signature → `401` [ran]:

```
GET /api/py/me -> {"id": "U1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "name": "Ada", ..., "created_at": "2026-10-04T20:48:42.908Z"}
GET /api/py/runs/<own run>/events -> {"events": [...2 events...]}
GET /api/py/runs/<other user's run>/events -> {"error": "Not found"} [404]
```

Cookie names to accept: `better-auth.session_token` and `__Secure-better-auth.session_token`
(HTTPS). If the app ever adopts Better Auth's `secrets` rotation (`BETTER_AUTH_SECRETS`,
present in 1.7.5 [src]), try each secret.

Password hashes are portable too [ran]: Better Auth = scrypt N=16384 r=16 p=1 dkLen=64,
NFKC-normalised password, `"<hex salt>:<hex key>"` where the salt *string* is the salt.
`hashlib.scrypt(nfkc(pw).encode(), salt=salt.encode(), n=16384, r=16, p=1, dklen=64,
maxmem=64 MiB)` verified a node-made hash (`True`). A custom Django hasher or auth backend can
check `account.password` directly when login moves to Django.

---

## 8. Migration ownership: managed=False now, `--fake-initial` later [ran]

Experiment: DB `foray_a` built from the four Drizzle SQL files; DB `foray_b` empty; same
models with `managed=False` removed; fresh `makemigrations`.

```
### foray_a (Drizzle-made): migrate --fake-initial
  Applying accounts.0001_initial... FAKED
  Applying api.0001_initial... FAKED
  Applying contenttypes.0001_initial... OK   ... auth ... sessions ... OK
### makemigrations --check on foray_a
No changes detected
```

Both apps were faked **even though my models deliberately modelled only a subset of columns
and had wrong types**. [src `MigrationExecutor.detect_soft_applied`]: for `CreateModel` it only
checks the table name exists; columns are checked only for `AddField`. And
`makemigrations --check` compares models to migration files, never to the DB.

`pg_dump --schema-only` diff, Drizzle-made (−) vs Django-made (+), abridged:

```
-    created_at timestamp without time zone DEFAULT now() NOT NULL,
+    created_at timestamp with time zone DEFAULT statement_timestamp() NOT NULL,   (before db_type fix)
-    level public.event_level DEFAULT 'info'::public.event_level NOT NULL,
+    level text DEFAULT 'info'::text NOT NULL,
-CREATE SEQUENCE public.agent_events_id_seq ... (bigserial)
+ALTER TABLE public.agent_events ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY
-    ADD CONSTRAINT session_token_unique UNIQUE (token);
+    ADD CONSTRAINT session_token_key UNIQUE (token);
-CREATE INDEX session_user_id_idx ON public.session USING btree (user_id);
+CREATE INDEX session_user_id_65cb3187 ON public.session USING btree (user_id);
+CREATE INDEX session_user_id_65cb3187_like ON public.session USING btree (user_id text_pattern_ops);
-CREATE UNIQUE INDEX companies_source_ats_slug_idx ... WHERE (ats_slug IS NOT NULL);
(Django FKs are DEFERRABLE INITIALLY DEFERRED; Drizzle's are immediate)
```

Recommended procedure:

1. **Transition**: Drizzle remains the only migrator; Django models `managed=False` (so
   `migrate` only creates Django's own tables). Any schema change is a Drizzle migration plus
   a hand edit of the Django model.
2. **Tests**: Django's test runner cannot build those tables:
   `django.db.utils.ProgrammingError: relation "user" does not exist` [ran]. Override
   `django_db_setup` in pytest-django to apply `drizzle/*.sql` into the test DB (or run tests
   against the dev DB with throwaway users like the Vitest suite does) until step 3.
3. **Flip ownership** (once no TS code issues DDL; plausibly as soon as the Python worker
   exists): remove `managed=False`, write models to match exactly (enum `db_type`s or
   `RunSQL` for `CREATE TYPE`, explicit `Meta.indexes`/`constraints` with Drizzle's names,
   `db_index=False` on FKs Drizzle indexed under another name, `UTCNaiveDateTimeField`),
   `makemigrations`, then `migrate --fake-initial` on every existing DB. **Gate it with a CI
   check**: build DB A from `drizzle/*.sql`, DB B from `manage.py migrate`, and diff
   `pg_dump --schema-only` (the script above). Expect to accept a few cosmetic diffs
   (`_like` indexes, deferrable FKs, identity vs serial) or encode them away.
4. Then stop running `pnpm db:migrate`; Better Auth (still on Drizzle until auth moves)
   keeps working because it only reads/writes rows.

---

## 9. Sessions vs signed cookies [doc + src, partly ran]

- `django.contrib.sessions.backends.db` (or `cached_db`): server-side, revocable
  (logout-everywhere, admin kill switch), cookie carries only a 32-char key, async methods
  exist; `alogin`/`aget_user`/`alogout` worked with the TEXT-pk user [ran]. **Recommended**
  when login moves to Django; one forced re-login at cutover is acceptable for this app. A
  seamless alternative is a custom `SessionStore` (or auth middleware) over Better Auth's
  existing `session` table — that is what §7.4 already does for reads.
- `signed_cookies`: no server state, so no revocation (a stolen cookie works until expiry),
  ~4 KB limit, data readable by the client, replayable after logout. 6.1 also changed its salt
  derivation (`SIGNED_COOKIE_LEGACY_SALT_FALLBACK`). Not a fit for an app whose current model
  (Better Auth) is DB sessions.

---

## 10. CSRF through Next rewrites [ran]

`CsrfViewMiddleware` with a valid `csrftoken` cookie + `X-CSRFToken`, POSTed via the Next
rewrite (browser Origin `http://127.0.0.1:3999`, upstream Host `127.0.0.1:8701`):

```
A default settings                          -> HTTP 403  "Origin checking failed - http://127.0.0.1:3999 does not match any trusted origins."
B CSRF_TRUSTED_ORIGINS=http://127.0.0.1:3999 -> HTTP 200  get_host 127.0.0.1:8701
C USE_X_FORWARDED_HOST=1                     -> HTTP 200  get_host 127.0.0.1:3999
C + no token                                 -> 403 "CSRF token missing."
C + Origin https://evil.example + valid token -> 403
```

Recommended settings: `CSRF_TRUSTED_ORIGINS = [APP_ORIGIN]` (explicit, no header trust), or
`USE_X_FORWARDED_HOST = True` only when Django is reachable *only* through Next/edge.
`SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")` only if the edge overwrites
XFP (Next forwards a client-supplied one). `CSRF_COOKIE_HTTPONLY = False` so the client can
read `csrftoken`; `SESSION_COOKIE_SAMESITE = "Lax"`.

django-ninja specifics [src]: ninja marks its views `csrf_exempt` at the Django middleware
level; CSRF is enforced only by cookie-based auth classes — `APIKeyCookie(csrf=True)` (default)
runs `CsrfViewMiddleware.process_request/process_view` via `ninja.utils.check_csrf`. So
cookie-authenticated ninja endpoints with unsafe methods must use `csrf=True`; I used
`csrf=False` only on GETs. Frontend: a `fetch` wrapper and the AI SDK `DefaultChatTransport`
`headers` option add `X-CSRFToken`. (Lighter alternative: SameSite=Lax + strict Origin
check only, which is Better Auth's own model; Django's token is defence in depth.)

---

## 11. API framework comparison

| | django-ninja 1.7.1 | DRF 3.18.1 | plain Django views |
|---|---|---|---|
| Async views | yes [ran] (`async def` ops, async auth `authenticate`) | **no** [ran]: `@api_view async def` → `AssertionError Expected a Response ... received coroutine`; `APIView.async get` → `'coroutine' object has no attribute 'status_code'`. (Third-party `adrf` exists; not evaluated.) | yes |
| Schemas | pydantic v2 (`Schema`), request validation, response models | serializers (sync) | hand-rolled |
| OpenAPI | 3.1, `manage.py export_openapi_schema --api ...` [ran]; cookie auth emitted as `apiKey in cookie`; SSE ops documented as `text/event-stream` | drf-spectacular (extra dep) | none |
| Streaming | `response=SSE[Model]` / `JSONL[Model]` with per-item validation [ran]; but only `no-cache` header (add `no-transform`), and its post-stream header copy is a no-op | sync `StreamingHttpResponse` | `StreamingHttpResponse(async gen)` [ran] |
| Fit | best: typed JSON API, async, TS types | heavy, sync-first | use for the chat stream and odd endpoints |

TypeScript types: OpenAPI 3.1 from ninja can feed `openapi-typescript` (npm `7.13.0`) or
`@hey-api/openapi-ts` (`0.99.0`) or `orval` (`8.40.0`) — versions from `npm view`; **I did not
run a generator** (no npm downloads were made). The exported schema is at
`research/django-core/openapi.json`.

---

## 12. Recommended layout inside the repo

```
backend/                      # uv project (uv init --app; pyproject + uv.lock + .python-version)
  pyproject.toml              # django~=6.1.1, django-ninja~=1.7, psycopg[binary,pool]~=3.3,
                              # uvicorn[standard]~=0.54, pydantic-settings~=2.15, dj-database-url~=3.1
                              # [dependency-groups] dev = pytest, pytest-django
  manage.py
  foray/                      # project package
    settings.py               # one module; values from foray/env.py
    env.py                    # pydantic-settings, mirrors src/env.ts (DATABASE_URL, BETTER_AUTH_SECRET, ...)
    asgi.py  urls.py
    api.py                    # NinjaAPI(), mounts routers from apps
    middleware.py             # SSE no-transform header; Better Auth cookie -> request.user
  apps/
    accounts/                 # User (db_table "user"), BetterAuthSession, Account, fields.py, better_auth.py
    runs/  jobs/  chat/  ...  # one app per domain, models db_table = existing tables
  tests/
```

`uv add` produced this pyproject [ran, `research/django-core/layout/backend`]:

```toml
requires-python = ">=3.14"
dependencies = ["dj-database-url~=3.1", "django~=6.1.1", "django-ninja~=1.7",
  "psycopg[binary,pool]~=3.3", "pydantic-settings~=2.15", "uvicorn[standard]~=0.54"]
[dependency-groups]
dev = ["pytest>=9.1.1", "pytest-django>=4.14.0"]
```

Settings essentials:

```python
DEBUG = env.debug
ALLOWED_HOSTS = [...]
INSTALLED_APPS = ["django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions",
                  "django.contrib.postgres", "ninja", "apps.accounts", ...]
AUTH_USER_MODEL = "accounts.User"
DATABASES = {"default": {**dj_database_url.parse(env.database_url, conn_max_age=0),
                         "OPTIONS": {"pool": {"min_size": 2, "max_size": 10, "timeout": 10}}}}
USE_TZ = True; TIME_ZONE = "UTC"
APPEND_SLASH = False
CSRF_TRUSTED_ORIGINS = [env.app_origin]
LOGGING = {"version": 1, "disable_existing_loggers": False, ...}
```

Run: `uv run uvicorn foray.asgi:application --reload --port 8000 --timeout-graceful-shutdown 5`
(dev, could join `pnpm dev:all`); prod `--workers N --proxy-headers
--timeout-graceful-shutdown 10` in a `python:3.14-slim` image as a new compose service.

---

## 13. Risks

- Exception inside an async streaming generator leaks a pooled DB connection (6.1.1, ran);
  every stream needs try/finally release.
- Long-lived streams pin one pooled connection each unless released; pool exhaustion surfaces
  as 500s after the pool timeout.
- Next's compression silently buffers any SSE without `no-transform` (django-ninja's SSE
  included).
- `--fake-initial` only checks table names; a wrong initial migration is accepted silently.
  Needs a schema-diff gate.
- No async transactions; approval/continuation and job-claim code must be sync functions
  under `sync_to_async`.
- Better Auth `timestamp` (naive) columns need the custom field or a column migration.
- uvicorn/granian wait forever on open streams at shutdown unless a timeout is configured.
- Django's logging config can hide errors (and uvicorn logs) unless
  `disable_existing_loggers=False`.
- uv picks managed CPython 3.14.5 over Homebrew 3.14.7 unless pinned/preferred.
- Next rewrites are resolved at build time with `output: "standalone"` (doc-level; not executed).

## 14. Open questions

- When does schema ownership flip: with the first Python worker slice, or after auth moves?
  (The TS worker never issues DDL, so it can be early.)
- Keep Better Auth's `session` table as the long-term session store (custom SessionStore),
  or cut over to `django_session` with one forced re-login?
- Full Django CSRF tokens (frontend change) vs Origin-only checks on the Django side?
- Generator choice for TS types (openapi-typescript vs hey-api) — another topic; not executed here.
- Whether to report the streaming-exception connection leak upstream.

## 15. Sources

- Django 6.1 release notes: https://docs.djangoproject.com/en/6.1/releases/6.1/
- Django 6.0 release notes: https://docs.djangoproject.com/en/6.1/releases/6.0/
- Django download / supported versions: https://www.djangoproject.com/download/
- Django 6.1 released (weblog): https://djangoproject.com/weblog/2026/aug/05/django-61-released
- Django async topic: https://docs.djangoproject.com/en/6.1/topics/async/
- Django tickets: https://code.djangoproject.com/ticket/35148 (dup of #35059, fixed),
  https://code.djangoproject.com/ticket/35059
- django-ninja releases: https://github.com/vitalik/django-ninja/releases (the summarizer's
  dates were wrong; PyPI says 1.7.1 was uploaded 2026-09-19)
- PyPI JSON API for all versions/dates (https://pypi.org/pypi/<name>/json)
- Installed source: `django/core/handlers/asgi.py`, `django/http/response.py`,
  `django/db/backends/postgresql/base.py`, `django/db/migrations/executor.py`,
  `ninja/streaming.py`, `ninja/operation.py`, `ninja/security/apikey.py`, `ninja/utils.py`,
  `rest_framework/views.py`
- Repo `node_modules` (read-only): `next/dist/server/lib/router-server.js`,
  `next/dist/compiled/compression`, `next/dist/docs/01-app/03-api-reference/05-config/01-next-config-js/rewrites.md`,
  `better-call@1.4.0/dist/crypto.mjs` + `context.mjs`, `@better-auth/utils@0.4.2/dist/password.node.mjs`,
  `@better-auth/core/dist/utils/id.mjs`, `better-auth/dist/cookies/index.mjs`

Scratch artifacts: `research/django-core/{proj,proj_fake,nextproxy,layout}`,
`inspectdb_full.py`, `schema_foray_a.sql`, `schema_foray_b.sql`, `openapi.json`, test scripts
`stamp.py gzstamp.py concurrency.py starve.py async_orm.py tz_check.py ssb.py drf_async.py
session_login.py sign.mjs`, server logs `*.log`.

---

## Verification (adversarial pass, 2026-10-04)

Scratch: `research/django-core-verify/` (`nextapp/` = standalone build on 16.3.5, `nextapp-plain/`
= same app without `output`, `nextapp-1638/` = standalone on the latest release,
`standalone-patched/`, `upstream.py`, `upstream_sse.py`, `stamp.mjs`, `*.log`; `venv/` = uv venv on
Homebrew Python 3.14.7, used only to run the stdlib upstream stubs).

Versions: `next` 16.3.5 (the repo's pin, installed fresh into the scratch app via pnpm, not
symlinked) and 16.3.8 (npm `latest` as of 2026-09-30; `pnpm view next dist-tags`), react/react-dom
19.2.8, Node 24, pnpm 10.34.5.

### Claim: "The Next rewrites destination is fixed at build time with `output: 'standalone'`, so the Django URL must be a build arg or a stable internal hostname."

**Verdict: CONFIRMED for `next.config` rewrites (by execution, 16.3.5 and 16.3.8). The scope is
wrong in one place: this has nothing to do with standalone. The "must" overstates it, because
there is a runtime path through `proxy.ts`.** Not refuted. The correction is below.

#### Experiment [ran]

`next.config.mjs` (scratch):
```js
const DJANGO = process.env.DJANGO_URL ?? "http://127.0.0.1:9199";
console.log("[next.config] evaluated, DJANGO_URL =", DJANGO);
export default {
  output: "standalone",
  outputFileTracingRoot: import.meta.dirname,
  async rewrites() {
    return {
      beforeFiles: [],
      afterFiles: [{ source: "/dj/:path*", destination: `${DJANGO}/dj/:path*` }],
      fallback: [{ source: "/api/:path*", destination: `${DJANGO}/api/:path*` }],
    };
  },
};
```
`proxy.js` (Next 16 proxy, Node runtime by default) reads the env var on each request:
```js
import { NextResponse } from "next/server";
export function proxy(request) {
  const upstream = process.env.DJANGO_URL ?? "http://127.0.0.1:9199";
  return NextResponse.rewrite(new URL(request.nextUrl.pathname + request.nextUrl.search, upstream));
}
export const config = { matcher: "/px/:path*" };
```
There are also `app/api/local/route.js`, which returns `{"servedBy":"next"}`, and two stdlib upstreams:
`BUILD` on :9101 and `RUNTIME` on :9102.

Built with `DJANGO_URL=http://127.0.0.1:9101 next build`. Nothing was listening on 9101 during the
build, so the build never contacts the destination. The server then ran with
`DJANGO_URL=http://127.0.0.1:9102`:

| Mode (16.3.5 unless noted) | `/api/live` (fallback) | `/dj/x` (afterFiles) | `/px/y` (proxy.ts) |
|---|---|---|---|
| `node .next/standalone/server.js` | **BUILD :9101** | **BUILD :9101** | RUNTIME :9102 |
| same, **16.3.8** | **BUILD :9101** | **BUILD :9101** | RUNTIME :9102 |
| `next start`, standalone build (warns it is unsupported) | **BUILD :9101** | **BUILD :9101** | RUNTIME :9102 |
| `next start`, **no `output`** (plain build) | **BUILD :9101** | **BUILD :9101** | RUNTIME :9102 |
| `next dev` | RUNTIME :9102 | RUNTIME :9102 | RUNTIME :9102 |

`/api/local` returned `{"servedBy":"next"}` in every mode. Under `next start`, the log line
`[next.config] evaluated, DJANGO_URL = http://127.0.0.1:9102` shows that the config is evaluated
again at startup. The rewrites still went to 9101.

#### Why [src, Next 16.3.5 `node_modules/next/dist`]

- `server/lib/router-utils/filesystem.js` ~L223-L300: outside dev, `customRoutes.rewrites` is
  `JSON.parse(readFile(distDir/routes-manifest.json)).rewrites`. Only the `else` branch, which is
  dev, calls `loadCustomRoutes(opts.config)`. Every production server reads rewrites from the
  manifest that `next build` wrote, standalone or not.
- `build/utils.js` ~L1128: standalone `server.js` embeds `const nextConfig = ${JSON.stringify(nextConfig)}`
  and sets `__NEXT_PRIVATE_STANDALONE_CONFIG`. `server/config.js` ~L1384 then returns that
  JSON without loading `next.config.*`, which is not copied into `.next/standalone`. The URL also
  appears in `server.js` under `_originalRewrites`, but routing does not use that copy (see the
  patch test below).
- The bundled `rewrites.md` the researcher cited says nothing about build time. The claim was
  right, but its source did not support it.

#### Corrections and additions [ran]

1. **It is not tied to standalone.** Every `next build` + production server freezes the
   destination in `.next/routes-manifest.json`. Only `next dev` re-reads it, at startup, so
   changing it needs a restart.
2. **`next build` loads `.env`.** With `DJANGO_URL=http://127.0.0.1:9177` in `.env` and nothing in the
   shell, the manifest got `http://127.0.0.1:9177/api/:path*` (`- Environments: .env`). The
   repo's `.dockerignore` excludes `.env`/`.env.*`, and compose passes `.env` only at runtime
   through `env_file`. So in the Docker `build` stage, `DJANGO_URL` is unset unless it is a build
   `ARG`/`ENV`, and the default in `next.config.ts` gets baked in. Make that default the
   in-network name (`http://api:8000`) and use `.env` for native dev.
3. **A runtime alternative exists: `proxy.ts`.** `NextResponse.rewrite(new URL(path, process.env.DJANGO_URL))`
   is read per request, including in the standalone server. Through it, on the standalone 16.3.5 server:
   - SSE (`text/event-stream`, `Cache-Control: no-cache, no-transform`, client sent
     `Accept-Encoding: gzip, deflate, br, zstd`): events at +0.031/+0.534/+1.039/+1.543 s with no
     `content-encoding`, so it is unbuffered. The first request after boot took 2.1 s before the
     first byte, which was a cold start; warm runs took about 0.03 s.
   - POST: the body was forwarded (`body={"q":1}` echoed in each event) and the stream was unbuffered.
   - Caveat [doc: bundled `proxy.md`, "Execution order"]: proxy runs before filesystem routes,
     so it does not give the "delete `route.ts` and the path falls through" behaviour of `fallback`.
     The matcher has to list the ported prefixes explicitly. That is arguably a clearer strangler
     switch, but the list must be kept in sync. The repo already has `src/proxy.ts`, an optimistic
     auth redirect matched on page paths; a Django branch would be added to it.
4. **A hack that works but is unsupported:** I changed 9101 to 9102 with `sed` in `.next/routes-manifest.json`
   only (a copy of the standalone output) and left `server.js` alone. The server then routed `/api/live`
   and `/dj/x` to 9102. A container entrypoint could template the manifest, but this relies on a
   private build artifact. Not recommended.
5. The hostname in a baked destination is resolved per request. The build did not need the
   upstream to be up. So a stable compose service name (`http://api:8000`) behind `fallback`
   rewrites is a sound choice for prod. Native dev uses `127.0.0.1:8000` from `.env`, which is
   evaluated when `next dev` or a local `next build` runs.

**Recommended reading of the claim:** keep the `fallback` rewrite with
`process.env.DJANGO_URL ?? "http://api:8000"`. Treat the value as build-time for every
production build. Reach for `proxy.ts` only if one image must point at different Django hosts
without a rebuild.

Sources: executed scratch apps above; `next/dist/server/lib/router-utils/filesystem.js`,
`next/dist/build/utils.js` (standalone `server.js` template), `next/dist/server/config.js`
(`__NEXT_PRIVATE_STANDALONE_CONFIG`), `next/dist/docs/01-app/03-api-reference/03-file-conventions/proxy.md`
("Runtime", "Execution order"), `next/dist/docs/.../05-config/01-next-config-js/output.md`;
`pnpm view next dist-tags` / `time`.
