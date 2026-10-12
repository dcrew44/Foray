"""`manage.py backup_legacy`: a verified backup of the old database and file tree (design §3.1).

Everything lands in `BACKUP_DIR` under one UTC stamp (`date -u +%Y%m%dT%H%M%SZ`):

1. `<stamp>-foray.dump`: `pg_dump -Fc` of the legacy database, run in the Postgres container.
2. `<stamp>-foray.list`: `pg_restore --list` of that dump, which must list `TABLE DATA` for every
   public table.
3. `<stamp>-legacy-data.tgz`: `tar -czf … -C LEGACY_STORAGE_DIR .`
4. `<stamp>-legacy-data.sha256`: one `<sha256>  ./<path>` line per file, sorted by path.
5. `<stamp>.ok`, written last: `{dump, dump_sha256, counts, created_at}` (the format step 0
   pinned: `dump` is the file name, `counts` maps every public table to its row count,
   `created_at` is ISO 8601 UTC with a `Z`).

The counts and the dump come from **one snapshot**: the counting transaction exports its snapshot
(`pg_export_snapshot()`) and `pg_dump --snapshot` reads exactly that, so the marker's counts are
the dump's counts even if something writes meanwhile. Nothing here writes to the legacy database
or the legacy tree. The importer refuses to run without a marker younger than 24 hours.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from apps.legacy import source

CONTAINER = "foray-db-1"  # the compose project is `name: foray`; its db service
MARKER_MAX_AGE = timedelta(hours=24)
MARKER_KEYS = ("dump", "dump_sha256", "counts", "created_at")

type Runner = Callable[..., subprocess.CompletedProcess[bytes]]


class BackupError(Exception):
    """A backup step failed; nothing was marked complete."""


class MarkerError(Exception):
    """No usable backup marker."""


def stamp_of(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def iso_z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class BackupFiles:
    stamp: str
    dump: Path
    listing: Path
    tarball: Path
    manifest: Path
    marker: Path

    @classmethod
    def under(cls, backup_dir: Path, stamp: str) -> BackupFiles:
        return cls(
            stamp=stamp,
            dump=backup_dir / f"{stamp}-foray.dump",
            listing=backup_dir / f"{stamp}-foray.list",
            tarball=backup_dir / f"{stamp}-legacy-data.tgz",
            manifest=backup_dir / f"{stamp}-legacy-data.sha256",
            marker=backup_dir / f"{stamp}.ok",
        )

    def all(self) -> list[Path]:
        return [self.dump, self.listing, self.tarball, self.manifest, self.marker]


@dataclass(frozen=True)
class BackupResult:
    files: BackupFiles
    counts: dict[str, int]
    dump_sha256: str
    manifest_lines: int


# --- the marker ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Marker:
    path: Path
    dump: str  # file name relative to BACKUP_DIR
    dump_sha256: str
    counts: dict[str, int]
    created_at: datetime

    @property
    def dump_path(self) -> Path:
        return self.path.parent / self.dump

    def age(self, now: datetime) -> timedelta:
        return now - self.created_at

    def summary(self) -> dict[str, Any]:
        return {
            "file": self.path.name,
            "dump": self.dump,
            "dump_sha256": self.dump_sha256,
            "created_at": iso_z(self.created_at),
        }


def read_marker(path: Path) -> Marker:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise MarkerError(f"{path.name} could not be read: {exc}") from None
    if not isinstance(data, dict) or set(data) != set(MARKER_KEYS):
        raise MarkerError(f"{path.name} does not hold exactly {{{', '.join(MARKER_KEYS)}}}.")
    dump, digest, counts, created = (data[key] for key in MARKER_KEYS)
    if not isinstance(dump, str) or "/" in dump or not dump:
        raise MarkerError(f"{path.name}: `dump` must be a file name in the backup directory.")
    if not isinstance(digest, str) or len(digest) != 64:
        raise MarkerError(f"{path.name}: `dump_sha256` must be a sha256 hex digest.")
    if not isinstance(counts, dict) or not all(
        isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
        for k, v in counts.items()
    ):
        raise MarkerError(f"{path.name}: `counts` must map table names to row counts.")
    try:
        if not isinstance(created, str) or not created.endswith("Z"):
            raise ValueError
        created_at = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        raise MarkerError(f"{path.name}: `created_at` must be ISO 8601 UTC with a Z.") from None
    return Marker(path, dump, digest, dict(counts), created_at)


def newest_marker(backup_dir: Path) -> Marker:
    markers = sorted(backup_dir.glob("*.ok")) if backup_dir.is_dir() else []
    if not markers:
        raise MarkerError(f"No backup marker in {backup_dir}. Run `pnpm db:backup-legacy` first.")
    return max((read_marker(path) for path in markers), key=lambda m: (m.created_at, m.path.name))


def fresh_marker(
    backup_dir: Path,
    *,
    now: datetime,
    max_age: timedelta | None = MARKER_MAX_AGE,
    check_dump: bool = True,
) -> Marker:
    """The newest marker, refused when it is older than `max_age` (None: any age) or when its
    dump is missing or does not hash to the recorded digest."""
    marker = newest_marker(backup_dir)
    if max_age is not None and marker.age(now) > max_age:
        hours = int(max_age.total_seconds() // 3600)
        raise MarkerError(
            f"The newest backup ({marker.path.name}, {iso_z(marker.created_at)}) is older than "
            f"{hours} hours. Run `pnpm db:backup-legacy` first."
        )
    if marker.created_at > now + timedelta(minutes=5):
        raise MarkerError(f"{marker.path.name} is dated in the future.")
    if check_dump:
        if not marker.dump_path.is_file():
            raise MarkerError(f"{marker.path.name} names {marker.dump}, which is missing.")
        if source.sha256_file(marker.dump_path) != marker.dump_sha256:
            raise MarkerError(f"{marker.dump} does not match the digest in {marker.path.name}.")
    return marker


# --- writing a backup ----------------------------------------------------------------------------


def _write_atomic(path: Path, data: bytes) -> None:
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def manifest_text(root: Path) -> str:
    """`sha256sum` lines for every file under `root`, as `./<path>`, sorted by path."""
    lines = [
        f"{source.sha256_file(path)}  ./{path.relative_to(root).as_posix()}"
        for path in source.walk_files(root)
        if path.is_file() and not path.is_symlink()
    ]
    return "".join(f"{line}\n" for line in lines)


def listed_table_data(listing: str) -> set[str]:
    """The public tables whose `TABLE DATA` entry a `pg_restore --list` output holds."""
    tables: set[str] = set()
    for line in listing.splitlines():
        if line.startswith(";") or " TABLE DATA " not in line:
            continue
        schema, _, rest = line.split(" TABLE DATA ", 1)[1].partition(" ")
        if schema == "public":
            tables.add(rest.split(" ", 1)[0])
    return tables


def _run(run: Runner, args: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
    try:
        result = run(list(args), **kwargs)
    except FileNotFoundError as exc:
        raise BackupError(f"Could not run {args[0]}: {exc}") from None
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", "replace").strip()
        raise BackupError(f"`{' '.join(args[:4])} …` failed ({result.returncode}): {detail}")
    return result


def backup_legacy(
    *,
    legacy_url: str,
    legacy_storage: Path,
    backup_dir: Path,
    container: str = CONTAINER,
    now: datetime | None = None,
    run: Runner = subprocess.run,
) -> BackupResult:
    """Write a dump, its listing, the tarball, the manifest and finally the marker."""
    moment = now or datetime.now(UTC)
    if not legacy_storage.is_dir():
        raise BackupError(f"LEGACY_STORAGE_DIR ({legacy_storage}) is not a directory.")
    if legacy_storage.resolve() in (backup_dir.resolve(), *backup_dir.resolve().parents):
        raise BackupError("BACKUP_DIR must not be inside LEGACY_STORAGE_DIR.")
    backup_dir.mkdir(parents=True, exist_ok=True)
    files = BackupFiles.under(backup_dir, stamp_of(moment))
    if any(path.exists() for path in files.all()):
        raise BackupError(f"A backup stamped {files.stamp} already exists.")

    parts = urlsplit(legacy_url)
    user = unquote(parts.username or "foray")
    dbname = source.database_name(legacy_url)
    try:
        with source.session(legacy_url, snapshot=True) as conn:
            row = conn.execute("SELECT pg_export_snapshot()").fetchone()
            assert row is not None
            snapshot_id = str(row[0])
            counts = source.table_counts(conn)
            # pg_dump reads the exported snapshot, which lives until this transaction ends.
            dump_part = files.dump.with_name(f".{files.dump.name}.part")
            with dump_part.open("wb") as out:
                _run(
                    run,
                    [
                        "docker",
                        "exec",
                        container,
                        "pg_dump",
                        "-U",
                        user,
                        "-Fc",
                        f"--snapshot={snapshot_id}",
                        dbname,
                    ],
                    stdout=out,
                    stderr=subprocess.PIPE,
                )
                out.flush()
                os.fsync(out.fileno())
            os.replace(dump_part, files.dump)
            conn.rollback()

        with files.dump.open("rb") as dump:
            listed = _run(
                run,
                ["docker", "exec", "-i", container, "pg_restore", "--list"],
                stdin=dump,
                capture_output=True,
            ).stdout
        _write_atomic(files.listing, listed)
        missing = set(counts) - listed_table_data(listed.decode("utf-8", "replace"))
        if missing:
            raise BackupError(
                f"The dump lists no TABLE DATA for {', '.join(sorted(missing))}; not marking it."
            )

        _run(
            run,
            ["tar", "-czf", str(files.tarball), "-C", str(legacy_storage), "."],
            capture_output=True,
        )
        manifest = manifest_text(legacy_storage)
        _write_atomic(files.manifest, manifest.encode())

        digest = source.sha256_file(files.dump)
        marker = {
            "dump": files.dump.name,
            "dump_sha256": digest,
            "counts": dict(sorted(counts.items())),
            "created_at": iso_z(moment),
        }
        _write_atomic(files.marker, (json.dumps(marker, indent=2) + "\n").encode())
    except BaseException:
        for path in files.all():
            path.unlink(missing_ok=True)
        files.dump.with_name(f".{files.dump.name}.part").unlink(missing_ok=True)
        raise
    return BackupResult(files, counts, digest, manifest.count("\n"))
