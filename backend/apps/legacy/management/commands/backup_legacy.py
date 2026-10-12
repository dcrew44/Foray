"""`pnpm db:backup-legacy`: dump, list, tar, manifest and mark a legacy backup (design §3.1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.legacy.backup import CONTAINER, BackupError, backup_legacy


class Command(BaseCommand):
    help = "Back up the legacy database and file tree into BACKUP_DIR, then write the .ok marker."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--container", default=CONTAINER, help=f"Postgres container (default {CONTAINER})"
        )

    def handle(self, *args: Any, container: str, **options: Any) -> None:
        env = settings.ENV
        if not env.legacy_database_url:
            raise CommandError("LEGACY_DATABASE_URL is not set. Point it at the old database.")
        if env.legacy_storage_dir is None:
            raise CommandError("LEGACY_STORAGE_DIR is not set. Point it at the old data/ folder.")
        try:
            result = backup_legacy(
                legacy_url=env.legacy_database_url,
                legacy_storage=Path(env.legacy_storage_dir),
                backup_dir=Path(settings.BACKUP_DIR),
                container=container,
            )
        except BackupError as exc:
            raise CommandError(f"Backup failed, nothing was marked: {exc}") from None
        files = result.files
        rows = sum(result.counts.values())
        self.stdout.write(f"Backup {files.stamp} in {files.marker.parent}:")
        self.stdout.write(f"  {files.dump.name}  sha256 {result.dump_sha256}")
        self.stdout.write(f"  {files.listing.name}  TABLE DATA for all {len(result.counts)} tables")
        self.stdout.write(f"  {files.tarball.name}")
        self.stdout.write(f"  {files.manifest.name}  {result.manifest_lines} files")
        self.stdout.write(f"  {files.marker.name}  {rows:,} rows in {len(result.counts)} tables")
        for table, count in result.counts.items():
            self.stdout.write(f"    {table:<16}{count:>10,}")
