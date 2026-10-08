"""Better Auth password carry-over (design §4.2; `map/auth.md` §4).

The legacy `account.password` is `<salt>:<key>`: a 32-character lowercase hex salt and the
128-character hex scrypt key of the NFKC-normalised password (N=16384, r=16, p=1, dkLen=64). The
salt passed to scrypt is **the hex string's own bytes**, not the 16 bytes it encodes. The import
stores it as `ba_scrypt$<salt>$<key>` (`encode_imported`), which Django routes to this hasher by
its prefix.

The hasher only verifies. `PBKDF2PasswordHasher` comes first in `PASSWORD_HASHERS`, and
`must_update` is always true, so the first successful sign-in re-hashes the password with PBKDF2
and saves it (`AbstractBaseUser.check_password`'s setter). Imported hashes disappear one login at
a time, with no flag day.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata
from typing import Any

from django.contrib.auth.hashers import BasePasswordHasher

ALGORITHM = "ba_scrypt"

# Better Auth 1.7 (`@better-auth/utils/password`): scrypt N=16384, r=16, p=1, dkLen=64.
SCRYPT_N = 16384
SCRYPT_R = 16
SCRYPT_P = 1
SCRYPT_DKLEN = 64
# Required: OpenSSL's 32 MiB default refuses r=16 ("memory limit exceeded"). Node uses 128*N*r*2.
SCRYPT_MAXMEM = 64 * 1024 * 1024

# Better Auth's password length rule (8 to 128), measured as JavaScript measures `.length`.
PASSWORD_MIN = 8
PASSWORD_MAX = 128

_SALT = re.compile(r"[0-9a-f]{32}")
_KEY = re.compile(r"[0-9a-f]{128}")
_LONE_SURROGATE = re.compile(r"[\ud800-\udfff]")


def _utf8(text: str) -> bytes:
    # Node encodes a lone UTF-16 surrogate as U+FFFD; Python's strict codec would raise.
    return _LONE_SURROGATE.sub("\ufffd", text).encode("utf-8")


def js_length(text: str) -> int:
    """`text.length` in JavaScript: UTF-16 code units (an emoji counts 2)."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def derive_key(password: str, salt: str) -> str:
    """Better Auth's `generateKey(password, salt)`, as lowercase hex."""
    key = hashlib.scrypt(
        _utf8(unicodedata.normalize("NFKC", password)),
        salt=salt.encode("utf-8"),  # the 32-character hex STRING's bytes, not the decoded bytes
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=SCRYPT_MAXMEM,
    )
    return key.hex()


def encode_imported(better_auth_hash: str) -> str:
    """A legacy `account.password` (`<salt>:<key>`) as stored on `users.password`. Raises
    `ValueError` for anything that is not a Better Auth hash."""
    salt, sep, key = better_auth_hash.partition(":")
    if not sep or not _SALT.fullmatch(salt) or not _KEY.fullmatch(key):
        raise ValueError("not a Better Auth scrypt hash")
    return f"{ALGORITHM}${salt}${key}"


def _split(encoded: str) -> tuple[str, str] | None:
    algorithm, _, rest = encoded.partition("$")
    salt, sep, key = rest.partition("$")
    if algorithm != ALGORITHM or not sep or not _SALT.fullmatch(salt) or not _KEY.fullmatch(key):
        return None
    return salt, key


class BetterAuthScryptHasher(BasePasswordHasher):
    """Verifies Better Auth 1.7 hashes imported as `ba_scrypt$<saltHex>$<keyHex>`. Never creates
    them."""

    algorithm = ALGORITHM

    def verify(self, password: str, encoded: str) -> bool:
        parts = _split(encoded)
        if parts is None:
            return False
        salt, key = parts
        return hmac.compare_digest(derive_key(password, salt), key)

    def encode(self, password: str, salt: str) -> str:
        raise NotImplementedError("import-only hasher")

    def decode(self, encoded: str) -> dict[str, Any]:
        parts = _split(encoded)
        if parts is None:
            raise ValueError("not a ba_scrypt hash")
        salt, key = parts
        return {"algorithm": self.algorithm, "salt": salt, "hash": key}

    def safe_summary(self, encoded: str) -> dict[str, Any]:
        return {"algorithm": self.algorithm}

    def must_update(self, encoded: str) -> bool:
        # Always: the first successful sign-in re-hashes with the preferred (PBKDF2) hasher.
        return True

    def harden_runtime(self, password: str, encoded: str) -> None:
        # Django calls this only when the stored hash's hasher is the preferred one, which this
        # never is. Unknown emails still run one PBKDF2 hash in ModelBackend (§4.2).
        return None
