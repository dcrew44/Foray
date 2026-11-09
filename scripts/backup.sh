#!/usr/bin/env sh
# pnpm backup: dump the database and archive the stored files into backups/, then drop all but
# the newest $KEEP of each (#54). Run it from cron on the machine that runs Foray.
#
# The database is dumped by the `db` container's own pg_dump. The files are either the prod
# profile's `app-storage` volume or, on a machine that runs the app natively, STORAGE_DIR.
set -eu

cd "$(dirname "$0")/.."
KEEP="${KEEP:-14}"
DATABASE="${FORAY_DATABASE:-foray_app}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p backups

docker compose exec -T db pg_dump -U foray -Fc "$DATABASE" > "backups/$DATABASE-$STAMP.dump.part"
mv "backups/$DATABASE-$STAMP.dump.part" "backups/$DATABASE-$STAMP.dump"
echo "database: backups/$DATABASE-$STAMP.dump"

if docker volume inspect foray_app-storage >/dev/null 2>&1 \
  && [ -n "$(docker compose --profile prod ps -q api 2>/dev/null)" ]; then
  docker run --rm -v foray_app-storage:/data:ro -v "$PWD/backups:/backup" alpine \
    tar czf "/backup/storage-$STAMP.tgz" -C /data .
else
  STORAGE="$(grep -E '^STORAGE_DIR=' .env 2>/dev/null | tail -1 | cut -d= -f2-)"
  STORAGE="${STORAGE:-./storage}"
  tar czf "backups/storage-$STAMP.tgz" -C "$STORAGE" --exclude=./rehearsal .
fi
echo "files:    backups/storage-$STAMP.tgz"

# Only this script's own files (a stamp like 20261109T223314Z): a dump named by hand stays.
for pattern in "$DATABASE-[0-9]*T[0-9]*Z.dump" "storage-[0-9]*T[0-9]*Z.tgz"; do
  # shellcheck disable=SC2012,SC2086
  ls -1t backups/$pattern 2>/dev/null | tail -n "+$((KEEP + 1))" | while read -r old; do
    rm -f "$old"
  done
done
