# Keeping Foray running

Foray is only as timely as its worker: boards are polled every fifteen minutes and new
postings are scored right after, but only while a worker is up. A laptop that runs
`pnpm dev:all` for an afternoon sees an afternoon's postings. There are two ways to keep one
running, and they can be combined.

## On the machine you develop on

```bash
pnpm worker:up      # migrate, then start a worker container that restarts with Docker
pnpm worker:logs
pnpm worker:down
```

The container runs the worker alone, against the same database and the same storage folder
as the native app, so everything it finds is there when you open Foray. It is built from the
checkout as it is when you start it: start it from `main`, and run `pnpm worker:up` again
after you pull.

Only one worker ever works on a database. While the background one is up, the worker inside
`pnpm dev:all` waits behind it (its log says so) and takes over if it stops. Work on a branch
that changes tasks or migrations belongs on the rehearsal stack (`pnpm rehearsal`), which has
its own database and so its own worker.

A laptop still sleeps. For postings found at 3 a.m., use a machine that stays on.

## On a machine that stays on

Any small Linux machine with Docker will do: a mini PC at home, or a 2 GB cloud server.

1. Clone the repository and write `.env` from `.env.example`. Set `SECRET_KEY`, the model keys
   and purposes you use, `SIGNUP_OPEN=false` once your own account exists, `DEBUG=false`, and
   `PUBLIC_ORIGIN` to the address you will open in a browser.
2. Start everything: `docker compose --profile prod up -d --build`. That is Postgres, the
   migrations, the api, the worker and the web server on port 3000, each restarting by itself.
3. Load the boards: `docker compose --profile prod exec api python manage.py seed_companies`.
4. Open `http://<the machine>:3000`, create your account, then set `SIGNUP_OPEN=false` and run
   step 2 again.
5. Reach it from your phone and laptop without opening it to the internet: put the machine
   on a private network such as Tailscale and use its name there as `PUBLIC_ORIGIN`. If you
   publish it instead, put a TLS proxy in front of port 3000 and set `TRUSTED_PROXY_HOPS=1`.

To move an existing install there, restore its backup instead of step 3:

```bash
docker compose up -d db
docker compose exec -T db pg_restore -U foray -d foray_app --clean --if-exists < backups/foray_app-<stamp>.dump
```

and unpack `storage-<stamp>.tgz` into the `foray_app-storage` volume before starting the rest.

## Backups

```bash
pnpm backup         # backups/foray_app-<stamp>.dump and backups/storage-<stamp>.tgz
```

It keeps the newest 14 of each (`KEEP=30 pnpm backup` for more). On the always-on machine, run
it nightly from cron and copy `backups/` somewhere else:

```
15 3 * * * cd /srv/foray && sh scripts/backup.sh >> backups/backup.log 2>&1
```

## Updating

```bash
git pull
docker compose --profile prod up -d --build     # or: pnpm worker:up
```

The migrations run before the api and the worker start.
