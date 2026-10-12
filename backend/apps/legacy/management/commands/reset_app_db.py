"""`manage.py reset_app_db`: drop, recreate and migrate the app database (design §3.2). DEBUG only.

It refuses unless the `DATABASE_URL` database is named `foray_app` or `foray_rehearsal` and passes
`assert_app_database` (§3.1), and once any import is marked cutover. The drop is not forced: a
database with other sessions open (a dev server, the worker) is refused by Postgres itself.
`STORAGE_DIR` is not touched.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS, connection, connections

from apps.core.guards import assert_app_database
from apps.legacy.rehearsal import RESETTABLE, ScratchError, admin_params, recreate_database


def cutover_recorded() -> bool:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT CASE WHEN to_regclass('public.legacy_imports') IS NULL THEN false "
            "ELSE EXISTS (SELECT 1 FROM legacy_imports WHERE cutover) END"
        )
        row = cur.fetchone()
    return bool(row and row[0])


class Command(BaseCommand):
    help = "DEBUG only: drop, recreate and migrate the app database (foray_app or foray_rehearsal)."

    def handle(self, *args: Any, **options: Any) -> None:
        if not settings.ENV.debug:
            raise CommandError("reset_app_db is for local development only (DEBUG=true).")
        name = str(connection.settings_dict["NAME"])
        if name not in RESETTABLE:
            raise CommandError(
                f'Refusing to reset "{name}": only {" or ".join(sorted(RESETTABLE))} may be reset.'
            )
        try:
            assert_app_database(connections[DEFAULT_DB_ALIAS])
        except ImproperlyConfigured as exc:
            raise CommandError(str(exc)) from None
        if cutover_recorded():
            raise CommandError(
                f'Refusing to reset "{name}": an import was marked as the cutover, so it holds '
                "the real data."
            )
        params = admin_params(connection.settings_dict)
        connections.close_all()
        close_pool = getattr(connection, "close_pool", None)
        if close_pool is not None:
            close_pool()
        try:
            recreate_database(name, params, allowed=RESETTABLE)
        except ScratchError as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(f'Dropped and recreated "{name}"; migrating.')
        call_command("migrate", interactive=False, verbosity=1)
        self.stdout.write(f'"{name}" is empty and migrated.')
