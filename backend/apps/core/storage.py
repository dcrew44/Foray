"""Where uploaded and rendered files live (design §11.7).

`LocalStorage(root)` exposes four methods, `put`, `open`, `delete` and `exists`, over opaque keys;
callers never build filesystem paths themselves, so S3 can later slot in behind the same four.

- **Keys** are `<user_id>/<kind>/<epoch_ms>-<safe filename tail, 100 chars>`, as today, so the
  `storage_path` values the legacy import copies keep working.
- **Path traversal:** a key that resolves outside the root (`..`, an absolute path, a symlink out)
  is refused.
- **Writes** go to a temporary file in the target directory, then `os.replace` it into place, so
  a reader never sees half a file and a crash never leaves one under the real key.
- **Sharing.** The API writes uploads and serves files; the worker writes rendered PDFs. Both use
  `STORAGE_DIR`: the same absolute directory in dev, a shared volume in prod.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import time
from pathlib import Path
from typing import BinaryIO

from django.conf import settings

__all__ = ["LocalStorage", "StorageError", "sha256_hex", "storage", "storage_key"]

_UNSAFE = re.compile(r"[^a-zA-Z0-9._-]")
_FILENAME_TAIL = 100


class StorageError(Exception):
    """A key that cannot be used (outside the root, empty)."""


def storage_key(user_id: str, kind: str, filename: str, *, now_ms: int | None = None) -> str:
    """`<user_id>/<kind>/<epoch_ms>-<safe filename tail>`: user-scoped, so one user's files
    cannot collide with another's (`storageKey`)."""
    safe = _UNSAFE.sub("_", filename)[-_FILENAME_TAIL:]
    stamp = int(time.time() * 1000) if now_ms is None else now_ms
    return f"{user_id}/{kind}/{stamp}-{safe}"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LocalStorage:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        if not key or "\x00" in key:
            raise StorageError("Refusing an empty storage key.")
        full = (self.root / key).resolve()
        if full == self.root or not full.is_relative_to(self.root):
            raise StorageError(f"Refusing to access a path outside the storage root: {key}")
        return full

    def put(self, key: str, data: bytes) -> str:
        """Stores `data` under `key` (temp file, then rename) and returns the key."""
        full = self._path(key)
        full.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=full.parent, prefix=".tmp-", suffix=".part")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, full)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise
        return key

    def open(self, key: str) -> BinaryIO:
        """The stored bytes as a binary file; the caller closes it. Raises FileNotFoundError."""
        return self._path(key).open("rb")

    def delete(self, key: str) -> None:
        """Removes the file. A file that is already gone is not an error."""
        self._path(key).unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()


def storage() -> LocalStorage:
    """The store under `STORAGE_DIR` (read at call time, so tests can point it elsewhere)."""
    return LocalStorage(settings.STORAGE_DIR)
