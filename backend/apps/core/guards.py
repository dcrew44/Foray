"""The schema-level guard against writing the legacy database (design §3.1, R16).

`env.py` already refuses a `DATABASE_URL` named `foray`, but the legacy database could sit under
another name, real environment variables beat `.env`, and the new table names coincide with
legacy ones. So every process that could write checks what it is connected to:

- the `pre_migrate` receiver (`apps/core/apps.py`), before `migrate` writes anything;
- the API at boot (`foray/asgi.py`) and `manage.py worker` at startup;
- `import_legacy` before its first write, and `reset_app_db` before dropping.

The check is read-only.
"""

from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured
from django.db.backends.base.base import BaseDatabaseWrapper

LEGACY_MARKERS_SQL = """
SELECT current_database(),
       to_regnamespace('drizzle') IS NOT NULL,
       to_regnamespace('pgboss') IS NOT NULL,
       to_regclass('public."user"') IS NOT NULL,
       to_regclass('public.profiles') IS NOT NULL
"""


def legacy_markers(connection: BaseDatabaseWrapper) -> tuple[str, list[str]]:
    """The connected database's name, and which legacy markers it has (empty when none)."""
    with connection.cursor() as cursor:
        cursor.execute(LEGACY_MARKERS_SQL)
        row = cursor.fetchone()
    assert row is not None
    name, drizzle, pgboss, user_table, profiles = row
    found = [
        label
        for label, present in (
            ("a drizzle schema", drizzle),
            ("a pgboss schema", pgboss),
            ('a public."user" table', user_table),
            ("a public.profiles table", profiles),
        )
        if present
    ]
    return name, found


def assert_app_database(connection: BaseDatabaseWrapper) -> None:
    """Raise `ImproperlyConfigured` when the connection points at a legacy-looking database."""
    name, found = legacy_markers(connection)
    if found:
        raise ImproperlyConfigured(
            f'Database "{name}" has {", ".join(found)}: it looks like the legacy Foray '
            "database; refusing to write to it."
        )
