"""`backup_legacy` and the backup marker (design §3.1; the format step 0 pinned).

The end-to-end test runs the real `docker exec … pg_dump` against a copy of the seeded legacy
database when the Postgres container is reachable, and is skipped otherwise.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from apps.legacy import source
from apps.legacy.backup import (
    CONTAINER,
    MARKER_KEYS,
    BackupError,
    MarkerError,
    backup_legacy,
    fresh_marker,
    listed_table_data,
    manifest_text,
    newest_marker,
    read_marker,
)
from tests.legacy import seed
from tests.legacy.conftest import LegacyEnv

NOW = datetime(2026, 10, 8, 21, 21, 34, tzinfo=UTC)


def write_marker(directory: Path, stamp: str, **fields: Any) -> Path:
    dump = directory / f"{stamp}-foray.dump"
    dump.write_bytes(b"dump " + stamp.encode())
    data = {
        "dump": dump.name,
        "dump_sha256": source.sha256_file(dump),
        "counts": {"jobs": 3, "user": 1},
        "created_at": f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[9:11]}:{stamp[11:13]}:"
        f"{stamp[13:15]}Z",
    }
    data.update(fields)
    path = directory / f"{stamp}.ok"
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path


def test_marker_reading_and_freshness(tmp_path: Path) -> None:
    with pytest.raises(MarkerError, match="No backup marker"):
        newest_marker(tmp_path)
    write_marker(tmp_path, "20261007T100000Z")
    write_marker(tmp_path, "20261008T212134Z")
    marker = fresh_marker(tmp_path, now=NOW + timedelta(hours=1))
    assert marker.path.name == "20261008T212134Z.ok"
    assert marker.created_at == NOW and marker.counts == {"jobs": 3, "user": 1}
    with pytest.raises(MarkerError, match="older than 24 hours"):
        fresh_marker(tmp_path, now=NOW + timedelta(hours=25))
    assert fresh_marker(tmp_path, now=NOW + timedelta(days=30), max_age=None) == marker
    marker.dump_path.write_bytes(b"changed")
    with pytest.raises(MarkerError, match="does not match the digest"):
        fresh_marker(tmp_path, now=NOW)
    marker.dump_path.unlink()
    with pytest.raises(MarkerError, match="which is missing"):
        fresh_marker(tmp_path, now=NOW)


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"extra": 1}, "does not hold exactly"),
        ({"dump": "../x.dump"}, "must be a file name"),
        ({"dump_sha256": "abc"}, "sha256 hex digest"),
        ({"counts": {"jobs": "3"}}, "map table names to row counts"),
        ({"created_at": "2026-10-08 21:21:34"}, "ISO 8601 UTC with a Z"),
    ],
)
def test_malformed_markers_are_refused(
    tmp_path: Path, fields: dict[str, Any], message: str
) -> None:
    path = write_marker(tmp_path, "20261008T212134Z", **fields)
    with pytest.raises(MarkerError, match=message):
        read_marker(path)


def test_manifest_and_listing_formats(tmp_path: Path) -> None:
    seed.write_files(tmp_path)
    lines = manifest_text(tmp_path).splitlines()
    assert lines == sorted(lines, key=lambda line: line.split("  ", 1)[1].encode())
    assert len(lines) == len(seed.FILES) + 1
    upload = seed.FILES["upload"]
    assert f"{upload.sha256}  ./{upload.key}" in lines
    assert manifest_text(tmp_path).endswith("\n")
    listing = (
        ";\n; Archive created at 2026-10-08 21:21:25 UTC\n"
        "3856; 0 16391 TABLE DATA drizzle __drizzle_migrations foray\n"
        "3857; 0 16581 TABLE DATA public account foray\n"
        "3859; 0 16613 TABLE DATA public user foray\n"
        "3858; 1259 16596 TABLE public session foray\n"
    )
    assert listed_table_data(listing) == {"account", "user"}


def test_a_failed_dump_leaves_no_marker(tmp_path: Path, legacy: LegacyEnv) -> None:
    def failing_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args, 1, b"", b"pg_dump: error: connection refused")

    backups = tmp_path / "out"
    with pytest.raises(BackupError, match="connection refused"):
        backup_legacy(
            legacy_url=legacy.url,
            legacy_storage=legacy.storage,
            backup_dir=backups,
            run=failing_run,
        )
    assert list(backups.iterdir()) == []


def test_backup_dir_inside_the_legacy_tree_is_refused(legacy: LegacyEnv) -> None:
    def never(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise AssertionError(f"nothing should run: {args}")

    inside = legacy.storage / "backups-here"
    with pytest.raises(BackupError, match="must not be inside LEGACY_STORAGE_DIR"):
        backup_legacy(
            legacy_url=legacy.url, legacy_storage=legacy.storage, backup_dir=inside, run=never
        )
    assert not inside.exists()


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "exec", CONTAINER, "pg_dump", "--version"], capture_output=True, check=False
    )
    return probe.returncode == 0


@pytest.mark.skipif(not _docker_ready(), reason=f"the {CONTAINER} container is not reachable")
def test_backup_legacy_end_to_end(legacy: LegacyEnv, tmp_path: Path) -> None:
    backups = tmp_path / "out"
    tables_before = source.fingerprints(legacy.url)
    files_before = source.file_fingerprints(legacy.storage)
    result = backup_legacy(
        legacy_url=legacy.url, legacy_storage=legacy.storage, backup_dir=backups, now=NOW
    )
    files = result.files
    assert files.stamp == "20261008T212134Z"
    assert sorted(p.name for p in backups.iterdir()) == sorted(
        [
            f"{files.stamp}-foray.dump",
            f"{files.stamp}-foray.list",
            f"{files.stamp}-legacy-data.tgz",
            f"{files.stamp}-legacy-data.sha256",
            f"{files.stamp}.ok",
        ]
    )

    marker = json.loads(files.marker.read_text())
    assert list(marker) == list(MARKER_KEYS)
    assert marker["dump"] == f"{files.stamp}-foray.dump"
    assert marker["dump_sha256"] == source.sha256_file(files.dump)
    assert marker["created_at"] == "2026-10-08T21:21:34Z"
    with source.session(legacy.url) as conn:
        assert marker["counts"] == source.table_counts(conn)
    assert len(marker["counts"]) == 18 and marker["counts"]["jobs"] == 6 + seed.FILLER_JOBS
    assert read_marker(files.marker).counts == marker["counts"]

    assert listed_table_data(files.listing.read_text()) == set(marker["counts"])
    assert files.manifest.read_text() == manifest_text(legacy.storage)
    with tarfile.open(files.tarball) as tar:
        names = set(tar.getnames())
    assert f"./{seed.FILES['upload'].key}" in names

    # Reading for the backup changed nothing on the legacy side.
    assert source.fingerprints(legacy.url) == tables_before
    assert source.file_fingerprints(legacy.storage) == files_before

    with pytest.raises(BackupError, match="already exists"):
        backup_legacy(
            legacy_url=legacy.url, legacy_storage=legacy.storage, backup_dir=backups, now=NOW
        )
