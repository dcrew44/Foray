"""Mail tokens at rest (#26): sealed before they are stored, opened only to be used.

A refresh token reads a user's mailbox for as long as Google honours it, so the database never
holds one in the clear: a dump, a backup or a `SELECT *` shows only ciphertext. `seal()` and
`unseal()` are Fernet (AES-128-CBC with an HMAC-SHA256 over the whole token) under a key derived
from `SECRET_KEY` with HKDF-SHA256 and a label of this module's own, so the key is never the
session-signing key itself and another use of `SECRET_KEY` cannot open these.

What follows from that:

- Whoever holds both the database and `SECRET_KEY` can read the tokens. The key lives in `.env`,
  not in the database, which is the separation this buys.
- **Changing `SECRET_KEY` makes every stored token unreadable.** `unseal()` then raises
  `Unreadable`, and the account asks to be connected again (`check.py`); nothing is retried.
- A sealed value starts with `v1:`, so a later scheme can tell its own from these.
"""

from __future__ import annotations

import base64
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings

__all__ = ["Unreadable", "seal", "unseal"]

VERSION = "v1"
# Domain separation: the key for this purpose and no other.
LABEL = b"foray.mail.tokens.v1"


class Unreadable(Exception):
    """A stored token this install's `SECRET_KEY` cannot open (the key changed, or the value
    was damaged). It carries no detail: there is nothing in it worth showing."""


@lru_cache(maxsize=4)
def _fernet(secret: str) -> Fernet:
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=LABEL).derive(secret.encode())
    return Fernet(base64.urlsafe_b64encode(key))


def seal(plain: str) -> str:
    """`plain` as text that is safe to store. Sealing the same token twice gives two values."""
    token = _fernet(settings.SECRET_KEY).encrypt(plain.encode()).decode("ascii")
    return f"{VERSION}:{token}"


def unseal(sealed: str) -> str:
    """The token `seal()` was given. Raises `Unreadable`, never anything that quotes it."""
    version, _, token = sealed.partition(":")
    if version != VERSION or not token:
        raise Unreadable
    try:
        return _fernet(settings.SECRET_KEY).decrypt(token.encode("ascii")).decode()
    except InvalidToken, UnicodeError, ValueError:
        raise Unreadable from None
