"""`manage.py mark_cutover`: mark the latest successful import as the cutover (design §3.2).

From then on `import_legacy --replace` and `reset_app_db` refuse: this database holds the real
data.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.legacy.importer import latest_record


class Command(BaseCommand):
    help = "Mark the latest successful legacy import as the cutover."

    def handle(self, *args: Any, **options: Any) -> None:
        record = latest_record()
        if record is None:
            raise CommandError("No successful import is recorded in this database.")
        stamp = f"{record.started_at:%Y-%m-%d %H:%M}Z"
        if record.cutover:
            self.stdout.write(f"The import of {stamp} from {record.legacy_db} is already marked.")
            return
        record.cutover = True
        record.save(update_fields=["cutover"])
        self.stdout.write(f"Marked the import of {stamp} from {record.legacy_db} as the cutover.")
