"""Id generators (design §2.5).

- New rows of uuid tables get `uuid7()` (index locality); imported rows keep their uuid4 values.
- Users keep Better Auth's shape: 32 random `[A-Za-z0-9]`, so legacy ids and storage keys stay
  valid side by side.
- Server-written chat messages get a 16-character alphanumeric id, like the AI SDK's
  `generateId()`.
"""

from __future__ import annotations

import secrets
import string
import uuid

ALPHANUMERIC = string.ascii_letters + string.digits


def new_id() -> uuid.UUID:
    return uuid.uuid7()


def random_alnum(length: int) -> str:
    return "".join(secrets.choice(ALPHANUMERIC) for _ in range(length))


def new_user_id() -> str:
    return random_alnum(32)


def generate_message_id() -> str:
    return random_alnum(16)
