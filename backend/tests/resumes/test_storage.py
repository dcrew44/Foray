"""Local storage (design §11.7): keys, the traversal guard, atomic writes."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from django.conf import settings

from apps.core.storage import LocalStorage, StorageError, sha256_hex, storage, storage_key


def test_keys_are_user_scoped_with_a_safe_filename_tail():
    key = storage_key("u" * 32, "resume_upload", "My Résumé (final).pdf", now_ms=1_790_000_000_000)
    assert key == f"{'u' * 32}/resume_upload/1790000000000-My_R_sum___final_.pdf"
    long = storage_key("u1", "resume_pdf", "x" * 150 + ".pdf", now_ms=1)
    assert long == f"u1/resume_pdf/1-{('x' * 150 + '.pdf')[-100:]}"


def test_put_open_exists_delete(tmp_path: Path):
    store = LocalStorage(tmp_path)
    key = "u1/resume_upload/1-a.pdf"
    assert store.exists(key) is False
    assert store.put(key, b"%PDF-1.7 hello") == key
    assert store.exists(key) is True
    with store.open(key) as handle:
        assert handle.read() == b"%PDF-1.7 hello"
    store.put(key, b"replaced")
    with store.open(key) as handle:
        assert handle.read() == b"replaced"
    store.delete(key)
    assert store.exists(key) is False
    store.delete(key)  # already gone: not an error
    with pytest.raises(FileNotFoundError):
        store.open(key)


def test_writes_leave_no_temporary_files(tmp_path: Path):
    store = LocalStorage(tmp_path)
    store.put("u1/k/1-a.pdf", b"x" * 10_000)
    assert sorted(os.listdir(tmp_path / "u1" / "k")) == ["1-a.pdf"]


def test_a_failed_write_leaves_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store = LocalStorage(tmp_path)

    def broken_replace(*_: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError, match="disk full"):
        store.put("u1/k/1-a.pdf", b"data")
    assert os.listdir(tmp_path / "u1" / "k") == []


@pytest.mark.parametrize(
    "key", ["../outside", "u1/../../outside", "/etc/passwd", "", ".", "u1/./../.."]
)
def test_refuses_keys_outside_the_root(tmp_path: Path, key: str):
    store = LocalStorage(tmp_path / "root")
    with pytest.raises(StorageError):
        store.put(key, b"x")
    with pytest.raises(StorageError):
        store.open(key)
    with pytest.raises(StorageError):
        store.exists(key)
    with pytest.raises(StorageError):
        store.delete(key)


def test_refuses_a_symlink_out_of_the_root(tmp_path: Path):
    root = tmp_path / "root"
    (root / "u1").mkdir(parents=True)
    (tmp_path / "secret").write_bytes(b"secret")
    (root / "u1" / "link").symlink_to(tmp_path / "secret")
    with pytest.raises(StorageError):
        LocalStorage(root).open("u1/link")


def test_the_default_store_is_storage_dir():
    assert storage().root == Path(settings.STORAGE_DIR).resolve()


def test_sha256_hex():
    assert sha256_hex(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
