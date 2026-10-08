# Job queue: replacing pg-boss 12 during the Django strangler

Research date 2026-10-04. Evidence labels: **[ran]** = executed here (scripts and logs are in
`scratchpad/research/job-queue/`), **[src]** = read in installed package source, **[live]** =
read-only query against the dev DB `foray-db-1`, **[docs]** = documentation or release notes only.

All experiments ran against a throwaway Postgres 18.6 container (`pgvector/pgvector:pg18`, the same
image as dev, port 55491, tmpfs, now stopped). Nothing was written to `foray-db-1` or the repo.

## 1. Recommendation

**Target: procrastinate 3.10.0 with `procrastinate.contrib.django`.** It is the only option that
covers everything Foray uses from pg-boss: per-queue concurrency, retries 0/1, cron, dedupe
locks, LISTEN/NOTIFY wake-ups, heartbeat-based stalled-job detection, graceful shutdown, sync and
async tasks, and deferring inside a Django `transaction.atomic()`. Its whole contract is plain SQL
functions, tables and an `AFTER INSERT` NOTIFY trigger, so TypeScript can enqueue a Python job with
one SQL statement.

- **Not Django Tasks (`django.tasks`) with `django-tasks-db`.** Django 6.1 core ships only the
  Immediate and Dummy backends. The DB backend moved to the separate `django-tasks-db` 0.13.0
  (Beta). Its worker polls once a second, runs one task per process, has no retries, no
  LISTEN/NOTIFY, no cron, no locks, and no heartbeat. A task whose worker was `kill -9`'d stayed
  `RUNNING` with nothing to recover it **[ran]**.
- **Not a hand-rolled SKIP LOCKED queue.** procrastinate already is one, in plain SQL that any
  language can call. A hand-rolled queue would reimplement roughly 15 features for no gain.

**Bridge: one owner per queue, and producers write into the owner's native table.**

- Python code enqueueing to a queue still worked by TypeScript does a plain SQL
  `INSERT INTO pgboss.job ... SELECT ... FROM pgboss.queue` plus `pg_notify`. The pg-boss 12
  worker picked these up in 22 ms **[ran]**.
- TypeScript code enqueueing to a queue now worked by Python does
  `INSERT INTO procrastinate_jobs ... ON CONFLICT DO NOTHING` (or calls
  `procrastinate_defer_jobs_v1`). The trigger NOTIFYs, and the Python worker started the job in
  4 to 10 ms **[ran]**.
- A queue's cutover commit:
  1. flips its owner in a shared routing table;
  2. moves any rows still queued in pg-boss with one SQL statement **[ran]**;
  3. unschedules any pg-boss cron for that queue.
- pg-boss is pinned and frozen until the last TypeScript consumer goes, then its schema is dropped.

## 2. Versions

| Package | Version | Released | Evidence |
|---|---|---|---|
| pg-boss (repo) | 12.33.2, schema `pgboss.version` = 42 | — | [src] node_modules, [live] |
| procrastinate | 3.10.0 | 2026-09-23 | [ran] uv install, PyPI JSON |
| Django | 6.1.1 (6.1 released 2026-08-05) | 2026-09-02 | [ran], [docs] |
| django-tasks (backport) | 0.12.0 | 2026-02-06 | [ran] |
| django-tasks-db | 0.13.0, "Development Status 4 - Beta", classifiers Django 5.2/6.0 | 2026-08-28 | [ran], PyPI |
| psycopg / psycopg-pool | 3.3.6 / 3.3.3 | 2026-09-18 | [ran] |
| Python | 3.14.7 (system `python3.14`) | — | [ran] |
| Node (shell) | v26.0.0 | — | [ran] |
| PostgreSQL | 18.6 (dev and scratch) | — | [live], [ran] |

procrastinate release history (PyPI upload dates): 3.7.0 2026-01-17 (dropped Python 3.9, added
3.14) · 3.8.0 2026-04-06 (atomic defer on external connections) · 3.9.0 2026-06-20 (task and worker
middleware; Django DB connection cleanup around each task) · 3.10.0 2026-09-23 (heartbeat
comparisons use the DB clock; cancel/status on external connections). The GitHub releases summary
gave 2024 years for these. PyPI's upload times are authoritative.

## 3. What Foray uses today (inventory)

Sources: `src/lib/queue/{boss,queues,send}.ts`, `src/worker/main.ts`, call sites **[src]**, and
the dev DB **[live]**.

| Queue | Policy | retryLimit | expire | Worker concurrency | Producers |
|---|---|---|---|---|---|
| ping | standard | 0 | 60 s | 1 | app/actions/ping.ts |
| company.discover | standard | 1 | 15 min | 2 | agents/tools/companies.ts |
| company.refresh | standard | 0 | 60 min | 1 | app/actions/companies.ts, cron `0 */6 * * *` key `company-refresh-cron`, `missed: "once"` |
| search | standard | 0 | 60 min | heavy (≤4) | app/actions/runs.ts, agents/tools/search.ts |
| search.evaluate | standard | 0 | 60 min | heavy | approval continuations (lib/runs/approvals.ts) |
| search.shortlist | standard | 0 | 5 min | heavy | approval continuations |
| match.evaluate | standard | 0 | 30 min | heavy | lib/runs/evaluate-run.ts |
| resume.tailor | standard | 0 | 30 min | heavy | lib/runs/tailor-run.ts |
| resume.render | standard | 1 | 10 min | 2 | lib/runs/tailor-run.ts |
| chat.follow_up | standard | 0 | 5 min | 2 | lib/runs/events.ts, with `singletonKey: follow-up-${event.id}` |

Plus a 10 s heartbeat (`system_state` key `worker`, stale after 45 s), a boot-time
`reapInterruptedRuns()`, `useListenNotify` on the worker only, and `batchSize: 1`.

Findings about the current code that matter for the migration:

1. **The follow-up `singletonKey` does nothing.** Every Foray queue is `standard` [live]. pg-boss
   only enforces `singletonKey` through policy-specific partial unique indexes (`job_i1` short,
   `job_i3` stately, `job_i6` exclusive), or through `job_i4` when `singletonSeconds` is set [src].
   Results [ran]:
   - `boss.send` twice with the same key gave two job ids on `standard` and on `singleton`.
   - Only `short`, `stately` and `exclusive` returned `null` for the second send.
   - Python sending twice to `chat.follow_up` with key `follow-up-7`: both jobs ran.

   (`exp1_singleton.log`, `exp1_worker.log`.) procrastinate's `queueing_lock` gives the dedupe the
   code intended.
2. **`runs.pgboss_job_id` is never written.** It is declared in the schema and in migration 0000,
   but nothing sets it [src]. The bridge can reuse it, renamed to `queue_job_id`, to tie a run to
   its job.
3. **The boot-time reaper becomes unsafe with two workers.** `reapInterruptedRuns()` marks every
   `running` run failed when the TS worker boots, and the TS worker restarts on every save in dev.
   Once Python works any run kind, a TS restart would fail Python's in-flight runs.
4. **The pg-boss cron lives in the database.** `pgboss.schedule` holds the
   `company.refresh / company-refresh-cron` row [live], and any pg-boss instance with
   `schedule: true` fires it. Deleting the `boss.schedule(...)` call is not enough to stop it: it
   needs `boss.unschedule('company.refresh', 'company-refresh-cron')` (or a DELETE on that row).
5. Approval continuations are stored as `{queue, payload}` and replayed through `send()` [src].
   This is already queue-agnostic, so routing them by owner needs no data change.

## 4. pg-boss 12 internals: can Python enqueue for the TS worker? Yes.

### Layout [src plans.js `create()`, live `\d`]

- Schema `pgboss`: tables `version` (42), `queue`, `schedule`, `subscription`, `bam`, `warning`,
  `queue_stats` (daily partitions), `job_dependency`, and **`job`, a table partitioned
  `BY LIST (name)`**. Its DEFAULT partition is `job_common`. A queue gets its own partition only
  with `partition: true`. All of Foray's queues have `partition=f, table_name=job_common` [live].
- Enum `pgboss.job_state`: `created, retry, active, completed, cancelled, failed`. The order
  matters: the fetch filter is `state < 'active'`.
- `job` columns: `id uuid default gen_random_uuid()`, `name`, `priority int 0`, `data jsonb`,
  `state`, `retry_limit int default 2`, `retry_count`, `retry_delay`, `retry_backoff`,
  `retry_delay_max`, `expire_seconds default 900`, `deletion_seconds default 604800`,
  `singleton_key`, `singleton_on timestamp`, `group_id`, `group_tier`,
  `start_after default now()`, `created_on`, `started_on`, `completed_on`,
  `keep_until default now()+14d`, `output`, `dead_letter`, `policy`, `heartbeat_on`,
  `heartbeat_seconds`, `blocked`, `blocking`, `pending_dependencies`, `source_*`.
  The PK is `(name, id)`. FKs `q_fkey` (name → queue) and `dlq_fkey` are DEFERRABLE INITIALLY
  DEFERRED.
- `pgboss.queue` holds per-queue policy: `policy, retry_limit, retry_delay, retry_backoff,
  expire_seconds, retention_seconds, deletion_seconds, notify, heartbeat_seconds, dead_letter...`.
  **pg-boss's own `send()` copies these onto the job row at insert time**
  (`insertJobs`: `COALESCE("retryLimit", q.retry_limit)` and so on). The column defaults
  (`retry_limit 2`, `expire 900`) are **not** Foray's policies. A naive
  `INSERT (name, data)` would silently retry model jobs twice.

### What the worker needs to pick a row up [src `fetchNextJob`]

`name = $queue AND state < 'active' AND NOT blocked AND start_after <= job_now()`, ordered
`priority DESC, created_on`, `FOR UPDATE SKIP LOCKED`. Nothing else is required.

### Wake-ups [src `notifier.js`, `notifyChannelSql`, `attorney.js`]

- The listener does `LISTEN` on `'pgboss_' || left(encode(sha224('pgboss'::bytea),'hex'),24)`.
  For this schema that is **`pgboss_454650302e472074f72063fd`** [live]. The payload is the queue
  name.
- With NOTIFY active on a queue, the worker's backstop poll relaxes to
  `notifyPollingInterval = max(30 s, pollingInterval)`. A row inserted without a NOTIFY therefore
  waits up to 30 s.

### Experiment 1 [ran]

`exp1_pgboss_worker.mjs` runs the repo's pg-boss 12.33.2 configured like `boss.ts`/`main.ts`.
`pgboss_send.py` / `exp1_py_producer.py` insert from Python with psycopg 3.3.6:

```sql
-- pgboss_send.py SEND_SQL (mirrors plans.insertJobs(notify=true) for one job)
WITH ins AS (
  INSERT INTO pgboss.job (id, name, data, priority, start_after, created_on,
    singleton_key, singleton_on, expire_seconds, deletion_seconds, keep_until,
    retry_limit, retry_delay, retry_backoff, retry_delay_max, policy, dead_letter, heartbeat_seconds)
  SELECT %(id)s, q.name, %(data)s::jsonb, COALESCE(%(priority)s, 0),
    now() + make_interval(secs => COALESCE(%(delay_seconds)s, 0)), now(),
    %(singleton_key)s, NULL, COALESCE(%(expire_seconds)s, q.expire_seconds), q.deletion_seconds,
    now() + make_interval(secs => COALESCE(%(delay_seconds)s, 0)) + q.retention_seconds * interval '1s',
    COALESCE(%(retry_limit)s, q.retry_limit), q.retry_delay, q.retry_backoff, q.retry_delay_max,
    q.policy, q.dead_letter, q.heartbeat_seconds
  FROM pgboss.queue q WHERE q.name = %(queue)s
  ON CONFLICT DO NOTHING
  RETURNING id, start_after, name
), notified AS (
  SELECT pg_notify('pgboss_' || left(encode(sha224('pgboss'::bytea), 'hex'), 24), ins.name)
  FROM ins JOIN pgboss.queue q ON q.name = ins.name
  WHERE %(notify)s AND q.notify AND ins.start_after <= now() LIMIT 1
)
SELECT id FROM ins WHERE (SELECT count(*) FROM notified) >= 0
```

| Case | Result |
|---|---|
| A. send + NOTIFY, autocommit | picked up in **22 ms**; worker saw `retryLimit 0, expireInSeconds 3600, policy standard` (inherited from the queue) |
| B. two sends, same `singleton_key`, standard queue | both inserted and both ran |
| C. `fail:true` on company.discover (retryLimit 1) | ran twice, final `failed retry_count 1`. The retry was picked up **30.0 s** later: a retry does not NOTIFY, so it waits for the relaxed poll |
| C. `fail:true` on search (retryLimit 0) | ran once, final `failed retry_count 0` |
| D. send inside a transaction, then rollback | never delivered |
| D. send inside a transaction, commit 2 s later | delivered **6 ms after COMMIT** (NOTIFY is transactional) |
| E. send without NOTIFY | picked up after **28.0 s** |

Conclusion: the plain SQL insert works. Required: copy policy columns from `pgboss.queue`, NOTIFY
on the hashed channel with the queue name, and have the queue row exist (otherwise the insert
returns no row; the Python helper must raise on `None`).

## 5. procrastinate 3.10.0

### Schema and migrations [src `sql/schema.sql`, ran `manage.py migrate`]

- The Django app `procrastinate.contrib.django` ships 41 Django migrations (0001…0041). They create
  their objects in `public` (unqualified SQL, so they follow `search_path`):
  - tables `procrastinate_jobs`, `procrastinate_events`, `procrastinate_periodic_defers`,
    `procrastinate_workers`;
  - enums `procrastinate_job_status` (`todo, doing, succeeded, failed, cancelled, aborting
    (legacy), aborted`) and `procrastinate_job_event_type`;
  - composite type `procrastinate_job_to_defer_v1`;
  - 18 functions (`procrastinate_defer_jobs_v1`, `_fetch_job_v2`, `_finish_job_v1`,
    `_retry_job_v2`, `_register_worker_v1`, `_update_heartbeat_v1`,
    `_prune_stalled_workers_v1`, …).

  No name collides with Foray's tables (`public.jobs` is the posting mirror) [live].
- `procrastinate_jobs`: `id bigserial`, `queue_name varchar(128)`, `task_name varchar(128)`,
  `priority int`, `lock text`, `queueing_lock text`, `args jsonb` (the task kwargs),
  `status`, `scheduled_at`, `attempts`, `abort_requested`, `worker_id → procrastinate_workers ON
  DELETE SET NULL`.
- Unique partial indexes: `queueing_lock WHERE status='todo'`, and `lock WHERE status='doing'`.
- NOTIFY comes from the trigger `procrastinate_jobs_notify_queue_job_inserted_v1`
  (`AFTER INSERT … WHEN status='todo'`). It sends on `procrastinate_queue_v1#<queue>` and
  `procrastinate_any_queue_v1`. **Any producer that inserts a row wakes the workers**, whatever
  its language.

### Behaviour, executed against Django 6.1.1 (scripts in `job-queue/djproj/`)

| Feature | Result |
|---|---|
| Defer from a sync Django view inside `transaction.atomic()` with a `Run` row | job started **4–6 ms** after commit; on rollback the job row never existed (ids skip 5) [ran] |
| Defer from an async view (`await task.defer_async(...)`) | started 3–4 ms later [ran] |
| Python `task.defer()` autocommit | pickup median 7 ms (6–20) [ran] |
| TS/Node `pg`: `SELECT unnest(procrastinate_defer_jobs_v1(ARRAY[ROW(...)]::procrastinate_job_to_defer_v1[]))` | pickup median 7 ms (4–8) [ran] |
| TS/Node `pg`: plain `INSERT INTO procrastinate_jobs … ON CONFLICT (queueing_lock) WHERE status='todo' DO NOTHING` | pickup 9–10 ms; trigger NOTIFY fired [ran] |
| Retries: default (`retry=False`) | 1 attempt, `failed` (= pg-boss retryLimit 0) [ran] |
| Retries: `retry=1` | 2 attempts, `failed attempts=2` (= retryLimit 1). The retry ran 5 s later (poll interval; a retry is an UPDATE, so no NOTIFY) [ran] |
| `queueing_lock` (≈ pg-boss `short`) | 2nd defer raises `AlreadyEnqueued`. Inside `atomic()` this aborts the whole transaction; wrap it in a nested `atomic()` savepoint and the outer transaction survives [ran]. Node: the INSERT form returns `null`; the function form raises `23505 procrastinate_jobs_queueing_lock_idx_v1` and aborts the caller's transaction [ran] |
| `lock` (≈ pg-boss `singleton`, but global) | 2nd `fetch_job_v2` returns nothing while one job holding the lock is `doing`; forcing a second `doing` violates `procrastinate_jobs_lock_idx_v1` [ran]. 3 jobs with one lock ran strictly in sequence [ran] |
| Per-queue concurrency | one `Worker` per queue group in one process (`foray_worker` command): peak 4 concurrent `search` (heavy=4), 2 `company.discover`, 1 `company.refresh`, isolated from each other [ran] |
| Stalled-job detection | worker `kill -9`'d mid-job at 20:55:45 (last heartbeat 20:55:43). A periodic `maintenance.reap_stalled` (every minute, `get_stalled_jobs(seconds_since_heartbeat=30)` then `finish_job(FAILED)`) failed it at **20:57:00**. Worst case ≈ 10 s heartbeat + 30 s + 60 s cron. Under pg-boss, `search` would have stayed active for its 60 min expiry [ran] |
| Graceful shutdown | SIGTERM during a 5 s job: the job finished, all 6 Workers unregistered, process exited 0.3 s after the job ended [ran] |
| Sync task using the ORM | ran in a pool thread (`sync_to_async(thread_sensitive=False)`); `DjangoApp` wraps it in `close_old_connections()` [ran, src `db_cleanup.py`] |
| Per-job timeout (pg-boss `expireInSeconds` for a live but hung job) | not built in. A 3-line async task middleware with `asyncio.timeout(limit)` failed a hung job at 2.05 s [ran] |
| Periodic | `@app.periodic(cron=..., periodic_id=...)`. Defers are idempotent per `(task, periodic_id, timestamp)` across workers via `procrastinate_periodic_defers`. On startup it defers only the latest missed tick, and only if within `max_delay` (default 600 s) [src `periodic.py`]. `company.refresh` (last tick 18:00, now 20:54) was correctly skipped and the minute-cron reaper was deferred [ran] |
| Connections | 6 Workers in one process = **10 PG connections** (psycopg pool min=max=4, plus one LISTEN connection per Worker) [ran] |

Details that matter when writing code against it:

- Concurrency is one `asyncio.Semaphore` per `Worker`, shared by all of its queues [src
  `worker.py`]. Per-queue limits mean one `Worker` per group. Several Workers can share a process
  (`asyncio.gather(app.run_worker_async(..., install_signal_handlers=False))`; executed). Each
  Worker's own handler would replace the previous one via `loop.add_signal_handler`, so install
  one SIGINT/SIGTERM handler that cancels them all.
- Use `app.replace_connector(app.connector.get_worker_connector())`, as the official management
  command does [src]. The Django connector can't LISTEN. `with_connector` is deprecated and does
  not carry the periodic registry.
- **Gotcha [ran]:** passing `task_middleware=[...]` to `run_worker_async` replaces the default
  Django DB-cleanup middlewares (`close_db_connections`, `close_db_connections_async`). Put extra
  middleware (the timeout one) in `PROCRASTINATE_WORKER_DEFAULTS["task_middleware"]`, which
  `with_db_cleanup()` appends after the cleanup ones.
- Stalled workers are pruned only at a Worker's startup. Their jobs keep `status='doing'` with
  `worker_id` set to NULL (FK ON DELETE SET NULL). `get_stalled_jobs` returns jobs whose worker is
  stale **or** NULL [src `queries.sql`]. No built-in auto-retry: you write the periodic reaper.
- procrastinate keeps finished jobs forever (`delete_jobs="never"`). pg-boss deleted them after
  7 days. Add the builtin `procrastinate.builtin_tasks.remove_old_jobs` as a periodic task, or run
  Workers with `delete_jobs="successful"`.
- Retries don't NOTIFY; they wait for `fetch_job_polling_interval` (default 5 s).

### Mapping pg-boss to procrastinate

| pg-boss (Foray use) | procrastinate |
|---|---|
| `retryLimit 0` | default `retry=False` |
| `retryLimit 1` | `retry=1` |
| `expireInSeconds` (dead worker) | heartbeat + `get_stalled_jobs` reaper (faster: about 100 s worst case vs 5–60 min) |
| `expireInSeconds` (hung job) | async task middleware with `asyncio.timeout` |
| `localConcurrency` per queue | one `Worker(queues=[...], concurrency=n)` per group |
| `useListenNotify` | on by default (`listen_notify=True`), trigger-driven |
| `singletonKey` + `short` policy | `queueing_lock` |
| `singleton` policy / concurrency 1 | `lock` (global across processes) |
| `schedule(cron, {missed:"once"})` | `@app.periodic(cron=…)`. Raise `max_delay` (PERIODIC_DEFAULTS) to 6 h and add a `queueing_lock` to approximate "once" [src, not executed] |
| deletion after 7 d | `remove_old_jobs` periodic |
| job id (uuid) | job id (bigint) |

## 6. Django Tasks (`django.tasks`) + `django-tasks-db`

- **Core [docs, src]:** Django 6.1 ships `django.tasks` with only `ImmediateBackend` and
  `DummyBackend`, "for development and testing only". Django handles definition and enqueueing,
  not execution. 6.1 added only `task()` kwargs forwarding and pickling. The API has `priority`,
  `queue_name`, `run_after`, `takes_context`, `enqueue/aenqueue`, and `TaskContext.attempt`. No
  retries, no scheduling. Its docs recommend `transaction.on_commit(partial(task.enqueue, ...))`.
- **DB backend [src, ran]:** the DB backend is now its own package, `django-tasks-db` (split out of
  `django-tasks` at 0.12.0). Its `db_worker` is a single loop:
  `select_for_update(skip_locked=True).first()`, then `claim`, then run, then sleep `--interval`
  (default 1 s). Measured pickup 83–773 ms.
  - One task at a time per process, and no concurrency option.
  - No retries (the failed task was `FAILED`, attempt 1) and no NOTIFY.
  - No periodic tasks and no locks.
  - `worker_ids` but no heartbeat table: a `kill -9`'d task stayed `RUNNING` indefinitely [ran].
  - `queue_name varchar(32)` and priority in −100..100.
  - Beta; classifiers stop at Django 6.0 (it worked on 6.1.1).
- **Other `django.tasks` backends [docs]:**
  - django-ox 1.8.0: first release 2026-08-16; retries with backoff.
  - dj-queue 0.14.2: Beta, first release 2026-04-08; LISTEN/NOTIFY + SKIP LOCKED.

  Both are too young to bet a migration on. No procrastinate `django.tasks` backend was found.
- Verdict: unsuitable as Foray's queue today. Foray could adopt the `@task` API later behind a thin
  adapter, but it buys nothing during the strangler.

## 7. Hand-rolled SKIP LOCKED queue

Foray needs fetch/claim, ack/fail, retry counts, a per-queue timeout, heartbeat + stalled
recovery, LISTEN/NOTIFY with a polling backstop, per-queue concurrency, graceful drain, dedupe
locks, cron with cross-process idempotence, retention cleanup, cross-language enqueue, admin
visibility and tests. procrastinate implements all of it in about 600 lines of SQL plus a small
async worker. Its tables and functions are already callable from TypeScript, which was the only
real argument for owning the schema.

Variant considered and rejected: **keep pg-boss as the bus and write a Python pg-boss
consumer**. The `pgboss` schema is owned and auto-migrated by the Node library (version 42,
migrated at TS worker boot). Expiry, retention, deletion, the cron pass and queue stats all run
in the Node supervisor. When the last Node process goes, Foray would have to re-own all of that.

## 8. Transition bridge design

### 8.1 Queue ownership

One routing table, read by both languages and flipped in the same commit that moves a handler.
A JSON file in the repo (`queue-owners.json`, e.g. `{"search": "ts", "chat.follow_up": "py"}`) is
simplest: it is versioned with the code. Task name, queue name and payload keys stay identical to
today's pg-boss names, so `args` is the pg-boss payload verbatim:
`@app.task(name="search", queue="search")`, then `def search(**payload)` (or a pydantic model).

### 8.2 TypeScript `send()` becomes a router (sketch; SQL executed in exp2_node_defer.mjs)

```ts
export async function send(queue, payload, options?) {
  if (OWNERS[queue] === "py") {
    // Runs on the app's pg pool; can share a Drizzle transaction with the run row.
    const { rows } = await pool.query(
      `INSERT INTO procrastinate_jobs (queue_name, task_name, priority, queueing_lock, args)
       VALUES ($1, $1, 0, $2, $3::jsonb)
       ON CONFLICT (queueing_lock) WHERE status = 'todo' DO NOTHING
       RETURNING id`,
      [queue, options?.singletonKey ?? null, JSON.stringify(payload)]);
    return rows[0] ? `procrastinate:${rows[0].id}` : null;
  }
  const id = await (await sender()).send(queue, payload, options ?? {});
  return id && `pgboss:${id}`;
}
```

Prefer the INSERT form over `procrastinate_defer_jobs_v1`. A dedupe collision then returns `null`
instead of raising 23505 and aborting the caller's transaction [ran].

### 8.3 Python `enqueue()` router (SQL executed in pgboss_send.py)

```python
def enqueue(queue: str, payload: dict, *, dedupe_key: str | None = None) -> str | None:
    if OWNERS[queue] == "ts":
        with connection.cursor() as cur:           # Django's connection, so atomic() applies
            jid = pgboss_send(cur, queue, payload, singleton_key=dedupe_key)  # INSERT…SELECT + pg_notify
        if jid is None: raise RuntimeError(f"pg-boss queue {queue} missing or rejected the job")
        return f"pgboss:{jid}"
    task = app.tasks[queue]
    try:
        with transaction.atomic():                 # savepoint: a dedupe miss must not kill the caller's tx
            return f"procrastinate:{task.configure(queueing_lock=dedupe_key).defer(**payload)}"
    except AlreadyEnqueued:
        return None
```

Note that a Python `singleton_key` sent to pg-boss dedupes nothing on `standard` queues, exactly
as today.

### 8.4 Cutover of one queue (one commit, app works before and after)

1. Add the Python task; flip `OWNERS[queue] = "py"` in both routers.
2. A Django data migration (idempotent) moves anything still queued in pg-boss. This was run
   against a runnable job, a delayed job and an in-flight job: only the first two moved, the delay
   was preserved, and the job then ran on procrastinate [ran `cutover.sql`]:
   ```sql
   WITH moving AS (
     SELECT name, id, data, priority, start_after FROM pgboss.job
      WHERE name = %(queue)s AND state < 'active' FOR UPDATE SKIP LOCKED
   ), ins AS (
     INSERT INTO procrastinate_jobs (queue_name, task_name, priority, args, scheduled_at)
     SELECT name, name, priority, COALESCE(data, '{}'::jsonb),
            CASE WHEN start_after > now() THEN start_after END FROM moving RETURNING id
   )
   UPDATE pgboss.job j SET state = 'cancelled', completed_on = now(),
          output = jsonb_build_object('movedTo', 'procrastinate')
     FROM moving m WHERE j.name = m.name AND j.id = m.id;
   ```
3. Keep the TS handler registered for one more slice, so jobs already `active` in TS finish. Then
   delete it.
4. For `company.refresh`: `boss.unschedule('company.refresh', 'company-refresh-cron')`, then add
   `@app.periodic` in Python.

### 8.5 Liveness, the reaper, and run ownership (do before the first Python handler ships)

- **Slice 0:** scope `reapInterruptedRuns()` to TS-owned run kinds (`kind = ANY(tsKinds)`).
  Otherwise every TS watch-mode restart fails Python's in-flight runs.
- Record the owning job on the run. Write `runs.pgboss_job_id`, renamed to `queue_job_id`, as
  `pgboss:<uuid>` or `procrastinate:<id>`; today nothing writes it. The Python reaper then fails
  only runs whose owning job is stalled.
- Do **not** derive this from `args.runId`: `chat.follow_up` carries the *parent* run's id, and
  reaping a stalled follow-up must not fail the search it reports on.
- The Python worker's reaper is the periodic `maintenance.reap_stalled` (executed). It replaces
  both pg-boss expiry and the boot-time reaper, using liveness rather than boot time.
- Worker-online banner: during the transition, have the Python worker also write a `system_state`
  key (e.g. `worker.py`) every 10 s from a side task, and make `workerStatus()` report each owner.
  End state: read `max(last_heartbeat) FROM procrastinate_workers` (already updated every 10 s)
  and delete the custom heartbeat.

### 8.6 End state

All consumers are on procrastinate and Next only renders. Remove `pg-boss` from `package.json`.
After confirming no `created/retry/active` rows remain, a migration runs
`DROP SCHEMA pgboss CASCADE`.

## 9. Risks

- The pg-boss pin is `^12.33.2` (caret). A pg-boss minor upgrade can migrate the `pgboss` schema
  at TS worker boot (version 42 today) and break the Python SQL shim. Pin the exact version for
  the transition. Have the Python shim check `SELECT version FROM pgboss.version` = 42 and refuse
  otherwise.
- Without the NOTIFY, a Python→pg-boss job waits up to 30 s (measured 28 s). Retries in both
  systems are poll-driven: 30 s in pg-boss, 5 s in procrastinate.
- `queueing_lock` only dedupes while the first job is still `todo`. For a true at-most-once
  follow-up per event, the handler must also check whether that event was already reported.
- The procrastinate tables live in `public`, next to Drizzle's tables. `drizzle-kit generate` diffs
  snapshots, not the live DB, so this is safe. Avoid `drizzle-kit push` / introspect, which would
  see them.
- Passing `task_middleware` directly to a Worker silently drops Django connection cleanup
  (verified). Configure it through `PROCRASTINATE_WORKER_DEFAULTS`.
- Sync tasks can't be interrupted by a timeout; only async tasks can. Write LLM-calling handlers as
  `async def`.
- Each procrastinate Worker holds a LISTEN connection: 6 groups = 10 connections per worker
  process. Budget `max_connections` (and avoid PgBouncer transaction pooling for LISTEN).
- `procrastinate_events` grows by about 3 rows per job; schedule `remove_old_jobs`.
- django-tasks-db classifiers stop at Django 6.0. That doesn't matter if it isn't chosen.

## 10. Open questions

- Does the ORM/auth research move Foray's schema ownership to Django migrations? If so,
  procrastinate's migrations simply join that graph. If Drizzle keeps ownership for a while, run
  `manage.py migrate procrastinate` as its own step in the prod `migrate` service.
- Should the cutover move run as a Django data migration (as proposed) or as an explicit
  management command run at deploy?
- What exactly should the `missed: "once"` cron do for `company.refresh`: is `max_delay` 6 h plus
  a `queueing_lock` acceptable? It defers the latest missed tick at boot, and at most one stays
  queued after a long sleep.
- For per-queue concurrency, one process with 6 Workers (10 connections, one crash domain), or
  separate processes per group?

## Sources

- pg-boss 12.33.2 source: `node_modules/.pnpm/pg-boss@12.33.2/node_modules/pg-boss/dist/{plans,manager,notifier,attorney,timekeeper}.js`
- procrastinate 3.10.0 source: `venv/lib/python3.14/site-packages/procrastinate/{worker,app,manager,periodic,retry,signals,utils}.py`, `sql/{schema,queries}.sql`, `contrib/django/*`
- django-tasks-db 0.13.0 source: `django_tasks_db/{models,backend}.py`, `management/commands/db_worker.py`; Django 6.1.1 `django/tasks/*`
- https://pypi.org/pypi/{procrastinate,django-tasks,django-tasks-db,django-ox,dj-queue}/json (versions, dates, classifiers, README)
- https://github.com/procrastinate-org/procrastinate/releases
- https://procrastinate.readthedocs.io/en/stable/howto/django.html
- https://docs.djangoproject.com/en/6.1/releases/6.1/ and https://docs.djangoproject.com/en/6.1/topics/tasks/
- https://www.djangoproject.com/community/ecosystem/ (Tasks section)
- https://www.loopwerk.io/articles/2026/django-tasks-review/
- https://forum.djangoproject.com/t/django-ox-a-database-backed-worker-for-the-new-tasks-framework/45777
- Experiment files: `scratchpad/research/job-queue/` (`exp1_*`, `exp2_*`, `exp3_*`, `pgboss_send.py`, `cutover.sql`, `locktest.sql`, `djproj/`)

## Verification (adversarial pass, 2026-10-04)

Two claims were marked as not executed. I tried to refute both against the repo's pinned pg-boss
(12.33.2, schema version 42) and the current npm `latest`, **12.36.0** (published by 2026-10-02,
schema version **44**). The repo's `^12.33.2` caret would resolve to 12.36.0 on a fresh lockfile.
Both were installed into `scratchpad/research/job-queue-verify/node-{pinned,latest}`. The 12.33.2
copy is byte-identical to the repo's `node_modules/.pnpm/pg-boss@12.33.2/.../dist/plans.js`
(`cmp`). The Python producer used psycopg 3.3.6 in `job-queue-verify/venv` (uv, CPython 3.14).

Experiments ran against a throwaway Postgres 18.6 container (`jq-verify-pg`,
`pgvector/pgvector:pg18`, tmpfs, port 55497), one database per pg-boss version. Against the dev
DB `foray-db-1` I ran only read-only `SELECT`s. Scripts and logs are in
`scratchpad/research/job-queue-verify/`.

Evidence labels are as above: **[ran]**, **[src]**, **[live]**.

### Claim 1: the `pgboss.job` column defaults are not Foray's policies. Verdict: CONFIRMED (not refuted)

- **[src]** In both versions, `createTableJob()` declares `retry_limit integer not null default 2`
  and `expire_seconds int not null default 900`, with `policy text` nullable and no default. Both
  values come from `QUEUE_DEFAULTS`, which is identical in 12.33.2 and 12.36.0. Nothing fills a
  row from `pgboss.queue` after the fact:
  - pg-boss creates no user triggers in the schema (`pg_trigger`, `NOT tgisinternal` → 0 rows
    [live]).
  - The only schema functions are `create_queue`, `delete_queue`, `job_now` (=`now()`) and
    `job_table_*`. There is no SQL-level `send`, so a non-Node producer has to write the policy
    itself [ran].
  - `insertJobs` is where `COALESCE("retryLimit", q.retry_limit)` and the other policy copies
    happen, in the JS-built statement. `failJobsBody` decides retry-vs-failed from the **row's**
    `retry_count < retry_limit`, and the worker's in-process timeout uses the **row's**
    `expire_seconds` (`manager.js`: `maxExpiration = max(job.expireInSeconds)` →
    `resolveWithinSeconds`). Supervisor expiry also uses the row's value (`failJobsByTimeout`:
    `started_on + expire_seconds`).
- **[live]** In `foray-db-1`, `information_schema.columns` reports `retry_limit` default 2,
  `expire_seconds` default 900 and `policy` default NULL. `pgboss.queue` has `search` at 0/3600 and
  `company.discover` at 1/900, and all queues are `standard`.
- **[ran] `exp1_retry.mjs` + `py_insert.py`.** A pg-boss worker (Foray's queue policies applied via
  `createQueue` + `updateQueue`) had handlers that always throw. Python inserted the jobs:

  ```sql
  -- naive
  INSERT INTO pgboss.job (name, data) VALUES (%(queue)s, %(data)s::jsonb) RETURNING ...
  -- policy-copying
  INSERT INTO pgboss.job (name, data, retry_limit, retry_delay, retry_backoff, retry_delay_max,
                          expire_seconds, deletion_seconds, keep_until, policy, dead_letter, heartbeat_seconds)
  SELECT q.name, %(data)s::jsonb, q.retry_limit, q.retry_delay, q.retry_backoff, q.retry_delay_max,
         q.expire_seconds, q.deletion_seconds, now() + q.retention_seconds * interval '1s',
         q.policy, q.dead_letter, q.heartbeat_seconds
    FROM pgboss.queue q WHERE q.name = %(queue)s
  ```

  Identical results on 12.33.2 (`exp1_pinned.log`) and 12.36.0 (`exp1_latest.log`):

  | queue (Foray retryLimit) | insert | stored row | handler runs | final |
  |---|---|---|---|---|
  | search (0) | naive | retry_limit 2, expire 900, policy NULL | **3** | failed, retry_count 2 |
  | company.discover (1) | naive | retry_limit 2, expire 900, policy NULL | **3** | failed, retry_count 2 |
  | search (0) | policy copy | retry_limit 0, expire 3600, standard | 1 | failed, retry_count 0 |
  | company.discover (1) | policy copy | retry_limit 1, expire 900, standard | 2 | failed, retry_count 1 |

  The worker's own metadata showed `retryLimit=2 expireInSeconds=900 policy=null` for the naive
  rows. A NULL `policy` does not stop a `standard` queue from fetching the row.
- **[ran] `exp1b_expire.mjs`.** Queue `exp.short` had `expireInSeconds 3, retryLimit 0`, and both
  handlers hung for 20 s:
  - The policy-copy job was failed at about 3.5 s with `handler execution exceeded 3s`.
  - The naive job was still `active` at 14 s, because its row says 900 s.
  - When the worker then stopped, the naive job went to `retry` with output
    `pg-boss shut down while active`, since `retry_count 0 < retry_limit 2`. The policy row would
    not. Foray's watch-mode TS worker restarts on every save, so a naive-inserted model job would
    also be **re-run after every dev restart**. This consequence is beyond the original claim.

  My first attempt used `batchSize: 2`, and both jobs stayed active. A batch's local timeout is
  the batch's maximum `expireInSeconds`, and `monitorIntervalSeconds` defaults to 60 s. That run
  was replaced by the `batchSize: 1` run in `exp1b_*.log`. Foray uses `batchSize: 1`.

Precision notes (not refutations):

- "Retry twice" means 3 attempts in total.
- The policy can be copied by `INSERT … SELECT … FROM pgboss.queue` or by literal values.
  INSERT…SELECT is better because it follows later `updateQueue` changes and, through the join,
  fails closed (0 rows) when the queue is missing.
- Expiry is also wrong for a naive insert: 900 s instead of 3600 s for `search` and
  `search.evaluate`. A search running longer than 15 min would be timed out and then retried twice.

### Claim 2: the `company.refresh` cron lives in `pgboss.schedule` and fires from any `schedule:true` instance until unscheduled or deleted. Verdict: CONFIRMED (not refuted), with a Foray-specific caveat

- **[live]** `foray-db-1` `pgboss.schedule` has the row `company.refresh | company-refresh-cron |
  0 */6 * * * | UTC | {"key":"company-refresh-cron","missed":"once"}`. Its `last_job_id` is a
  `company.refresh` job created `2026-10-04 18:02:47Z`, now `completed`, so the row is live and
  firing.
- **[src]** In both versions:
  - `attorney.getConfig` sets `config.schedule = ('schedule' in config) ? config.schedule : true`.
    **The default is true.**
  - `index.start()` calls `timekeeper.start()` when `config.schedule` is set.
  - `Timekeeper.start()` works the internal queue `__pgboss__send-it` (that queue exists in
    `foray-db-1` [live]) and arms `onCron`.
  - `onCron` → `cron()` → `getSchedules()` = `SELECT … FROM pgboss.schedule ORDER BY name, key`.
    This reads **every row**, regardless of who wrote it or whether this instance registered a
    handler for the queue.
  - `unschedule(name, key = '')` is `DELETE FROM schedule WHERE name = $1 AND COALESCE(key,'') = $2`.
  - Passes are deduplicated across instances by a claim on `pgboss.version.cron_on`
    (`trySetCronTime`) and by singleton slots on the send-it queue, so several `schedule:true`
    instances do not multiply jobs.
- **[ran] `exp2_cron.mjs`** used an every-minute cron (`* * * * *`) in place of `0 */6 * * *` so
  it would fire during the run. Identical results on 12.33.2 (`exp2_pinned.log`) and 12.36.0
  (`exp2_latest.log`):
  1. Instance A, with `schedule:false, supervise:false`, called
     `schedule('company.refresh', '* * * * *', {}, {key:'company-refresh-cron', missed:'once'})`
     and stopped. A never ran a cron pass, but the row was written.
  2. Only a sender-like instance was running (`schedule:false`, as Foray's Next app is). Across a
     minute boundary, `company.refresh` jobs went **0 → 0**.
  3. Instance B was created with **default config**. It never called `schedule()` and registered
     no `company.refresh` handler. It created a job right after start (21:10:09, the due window)
     and another at the next boundary (21:11:01). **jobs = 2**, both with data `{}`.
  4. `B.unschedule('company.refresh')` without the key: **the row survived**, because the key
     defaults to `''`.
  5. `B.unschedule('company.refresh', 'company-refresh-cron')`: row gone. B kept running across
     the next minute boundary: jobs **2 → 2** (no new job; 2 send-it jobs total, matching the 2 refresh jobs).
  6. The row was re-created with `schedule()` and removed with a plain SQL `DELETE FROM
     pgboss.schedule WHERE name=… AND key=…`. Across the next boundary: jobs **2 → 2**.

Caveats and precision:

- **Foray re-creates the row on every TS worker boot.** `src/worker/main.ts:119` calls
  `boss.schedule(QUEUES.companyRefresh, "0 */6 * * *", {}, {key: "company-refresh-cron",
  missed: "once"})` after every `boss.start()`. Unscheduling (or deleting the row) while that call
  still ships is undone at the next TS worker start, which in dev is every save. The cutover must
  do both, in this order:
  1. remove the `schedule()` call;
  2. once no TS worker that still has the call is running, `unschedule(name, key)` or `DELETE` the
     row.

  The reverse also holds, as the researcher's §3.4 says: removing the call alone leaves the row
  firing.
- "Any instance with `schedule:true`" includes any `new PgBoss(...)` built **without** options.
  `schedule`, `supervise` and `migrate` all default to true. An ad-hoc script that constructs pg-boss
  with defaults would fire the cron and also migrate the schema. Foray's `createBoss` passes
  `schedule: role === "worker"`, so today only the worker fires it.
- `deleteQueue('company.refresh')` also removes the row (`schedule.name REFERENCES queue ON DELETE
  CASCADE` [src]). In the end state, `DROP SCHEMA pgboss CASCADE` removes it too. Once no
  `schedule:true` process is left, the row is inert.
- The `key` is required: `unschedule('company.refresh')` silently deletes nothing.
