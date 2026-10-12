"""`manage.py legacy_rehearsal {restore,drop}`: the import rehearsal's scratch side (step 3F).

    legacy_rehearsal restore [--store DIR] [--container NAME]
        Restore the newest backup's dump into foray_restore (its digest checked first), create an
        empty foray_rehearsal, and optionally a scratch STORAGE_DIR.
    legacy_rehearsal drop [--store DIR]
        Drop foray_rehearsal and foray_restore, and remove that scratch store.

Nothing outside the rehearsal allowlist is ever created or dropped (`apps.legacy.rehearsal`).
The rehearsal itself is then

    DATABASE_URL=…/foray_rehearsal manage.py migrate
    DATABASE_URL=…/foray_rehearsal LEGACY_DATABASE_URL=…/foray_restore STORAGE_DIR=DIR \\
        manage.py import_legacy --rehearsal
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import connection
from psycopg.conninfo import make_conninfo

from apps.legacy import source
from apps.legacy.backup import CONTAINER, MarkerError, fresh_marker
from apps.legacy.rehearsal import (
    REHEARSAL,
    REHEARSAL_DB,
    RESTORE_DB,
    ScratchError,
    admin_params,
    create_database,
    create_store,
    drop_database,
    drop_store,
    restore_dump,
)


class Command(BaseCommand):
    help = "Create or drop the legacy import rehearsal's scratch databases and store."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("action", choices=["restore", "drop"])
        parser.add_argument("--store", type=Path, help="a scratch STORAGE_DIR to create or remove")
        parser.add_argument("--container", default=CONTAINER)

    def handle(self, *args: Any, action: str, store: Path | None, container: str, **_: Any) -> None:
        params = admin_params(connection.settings_dict)
        try:
            if action == "restore":
                self.restore(params, store, container)
            else:
                self.drop(params, store)
        except (ScratchError, MarkerError) as exc:
            raise CommandError(str(exc)) from None

    def restore(self, params: dict[str, Any], store: Path | None, container: str) -> None:
        marker = fresh_marker(Path(settings.BACKUP_DIR), now=datetime.now(UTC), max_age=None)
        self.stdout.write(f"Restoring {marker.dump} into {RESTORE_DB}.")
        restore_dump(marker.dump_path, params=params, container=container)
        with source.session(make_conninfo(dbname=RESTORE_DB, **params)) as conn:
            counts = source.table_counts(conn)
        if counts != marker.counts:
            lines = source.diff_mappings(marker.counts, counts)
            raise CommandError("The restored counts differ from the marker: " + "; ".join(lines))
        self.stdout.write(f"{RESTORE_DB}: {sum(counts.values()):,} rows, equal to the marker.")
        create_database(REHEARSAL_DB, params, allowed=REHEARSAL)
        self.stdout.write(f"Created an empty {REHEARSAL_DB}.")
        if store is not None:
            create_store(store)
            self.stdout.write(f"Created the scratch store {store}.")

    def drop(self, params: dict[str, Any], store: Path | None) -> None:
        for name in (REHEARSAL_DB, RESTORE_DB):
            existed = drop_database(name, params, allowed=REHEARSAL)
            self.stdout.write(f"{'Dropped' if existed else 'No'} {name}.")
        if store is not None:
            protected = [Path(settings.STORAGE_DIR)]
            if settings.ENV.legacy_storage_dir is not None:
                protected.append(Path(settings.ENV.legacy_storage_dir))
            removed = drop_store(store, protected=protected)
            self.stdout.write(f"{'Removed' if removed else 'No'} scratch store {store}.")
