"""Checking a mailbox for employers' answers (#26). Async: the requests to Google happen here,
and every database step is a sync function of `store.py` run through `db()`.

One check of one account:

1. Open the stored tokens (`store.open_account`). No access token to reuse: ask Google for one
   with the refresh token, once.
2. List the ids `google.QUERY` finds, newest first, a page at a time, and keep those not stored
   yet (`store.unseen`), until there are `store.LIMIT` of them or the listing ends.
3. Read each new message's metadata, and nothing else of it (`read_message`).
4. `store.record` classifies, matches and stores them; `store.finish` notes the check.

**It only reads.** Every request to Gmail is a GET for a listing or for one message's metadata.

**A token Google no longer honours** (`google.Reconnect`: revoked, expired, or missing the
scope) marks the account as needing to be reconnected and stops: nothing asks Google again
until the user connects again. An access token Gmail refuses is refreshed once; refused again,
the account is treated the same way.

**Anything else** (Google unreachable, a rate limit, the API not enabled) is the sentence
Settings shows until a check succeeds. What was read before the failure is kept, and the next
scheduled check carries on from there: no retry inside this one.

Nothing here logs a token, a subject line or an address: log lines carry the user's id, counts
and `GoogleError.code`.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any

from apps.core.db import db
from apps.core.http import HttpClient
from apps.mail import google, store
from apps.mail.classify import clean
from apps.mail.models import SNIPPET_MAX

__all__ = ["ACCOUNT_TIMEOUT_S", "MAX_PAGES", "Outcome", "check_all", "check_user", "read_message"]

log = logging.getLogger("foray.mail")

MAX_PAGES = 10  # listing pages looked through for unread ids, 100 ids each
ACCOUNT_TIMEOUT_S = 120.0

UNEXPECTED = "The last check failed unexpectedly; the worker's log has the details."
TOO_SLOW = "The last check took too long and was stopped. Foray will try again at the next one."


@dataclass(slots=True)
class Outcome:
    checked: int = 0  # accounts whose mailbox was reached
    stored: int = 0  # messages newly stored
    matched: int = 0  # of them (or of earlier ones), matched to an application
    failed: int = 0  # accounts whose check did not finish


# --- one message ---------------------------------------------------------------------------------


def _decoded(value: str) -> str:
    """A header as text: RFC 2047 encoded words decoded when there are any."""
    if "=?" not in value:
        return value
    try:
        return str(make_header(decode_header(value)))
    except ValueError, LookupError, UnicodeError:
        return value


def _sender(value: str) -> tuple[str, str]:
    """`(display name, domain)` of a From header. The local part of the address is dropped
    here and never stored."""
    name, address = parseaddr(value)
    domain = address.rpartition("@")[2].strip().lower().rstrip(".") if "@" in address else ""
    return clean(_decoded(name)).strip("\"' "), domain


def _received_at(message: dict[str, Any], headers: dict[str, str]) -> datetime:
    """When Gmail received it (`internalDate`, epoch milliseconds), else its Date header, else
    now: a message is stored once whatever its date says."""
    internal = message.get("internalDate")
    try:
        return datetime.fromtimestamp(int(internal) / 1000, tz=UTC)  # type: ignore[arg-type]
    except TypeError, ValueError, OverflowError, OSError:
        pass
    try:
        parsed = parsedate_to_datetime(headers.get("date", ""))
    except TypeError, ValueError:
        return datetime.now(UTC)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def read_message(message: dict[str, Any]) -> store.Incoming | None:
    """What Foray keeps of a message, from Gmail's `format=metadata` answer; None when it has
    no id. Reads `id`, `internalDate`, `snippet` and the From, Subject and Date headers."""
    message_id = message.get("id")
    if not isinstance(message_id, str) or not message_id:
        return None
    payload = message.get("payload")
    listed = payload.get("headers") if isinstance(payload, dict) else None
    headers: dict[str, str] = {}
    for header in listed if isinstance(listed, list) else []:
        if isinstance(header, dict):
            name, value = header.get("name"), header.get("value")
            if isinstance(name, str) and isinstance(value, str):
                headers.setdefault(name.lower(), value)
    sender_name, sender_domain = _sender(headers.get("from", ""))
    snippet = message.get("snippet")
    return store.Incoming(
        message_id=message_id,
        received_at=_received_at(message, headers),
        sender_name=sender_name,
        sender_domain=sender_domain,
        subject=clean(_decoded(headers.get("subject", ""))),
        snippet=clean(snippet if isinstance(snippet, str) else "")[:SNIPPET_MAX],
    )


# --- one account ---------------------------------------------------------------------------------


async def _fresh_token(client: HttpClient, access: store.Access) -> str:
    tokens = await google.refresh_access_token(client, access.refresh_token)
    await db(store.save_access_token, access.user_id, access.account_id, tokens)
    return tokens.access_token


async def _new_ids(client: HttpClient, token: str, access: store.Access) -> list[str]:
    """Up to `store.LIMIT` ids the search finds that are not stored yet, newest first."""
    found: list[str] = []
    page: str | None = None
    for _ in range(MAX_PAGES):
        ids, page = await google.list_message_ids(client, token, page)
        if ids:
            found += await db(store.unseen, access.user_id, access.account_id, ids)
        if len(found) >= store.LIMIT or page is None:
            break
    return list(dict.fromkeys(found))[: store.LIMIT]


async def _read(
    client: HttpClient, token: str, access: store.Access, incoming: list[store.Incoming]
) -> None:
    for message_id in await _new_ids(client, token, access):
        try:
            message = await google.message_metadata(client, token, message_id)
        except google.Gone:
            continue  # deleted since it was listed
        read = read_message(message)
        if read is not None:
            incoming.append(read)


async def _check(client: HttpClient, access: store.Access, outcome: Outcome) -> None:
    incoming: list[store.Incoming] = []
    error = ""
    try:
        token = access.access_token or await _fresh_token(client, access)
        try:
            await _read(client, token, access, incoming)
        except google.Unauthorized:
            if access.access_token is None:
                raise  # that token was new: asking again would not help
            del incoming[:]
            await _read(client, await _fresh_token(client, access), access, incoming)
    except (google.Reconnect, google.Unauthorized) as refused:
        sentence = str(refused)
        log.warning("mail: user %s has to reconnect (%s)", access.user_id, refused.code)
        await db(store.need_reconnect, access.user_id, access.account_id, sentence)
        outcome.failed += 1
        return
    except google.GoogleError as failed:
        error = str(failed)
        log.warning("mail: check for user %s stopped (%s)", access.user_id, failed.code)

    # Also with nothing new: earlier messages are matched again against what was sent since.
    recorded = await db(store.record, access.user_id, access.account_id, incoming)
    outcome.stored += recorded.stored
    outcome.matched += recorded.matched
    await db(store.finish, access.user_id, access.account_id, error)
    if error:
        outcome.failed += 1
    else:
        outcome.checked += 1


async def _check_account(user_id: str, account_id: uuid.UUID, outcome: Outcome) -> None:
    access = await db(store.open_account, user_id, account_id)
    if access is None:
        return
    try:
        async with asyncio.timeout(ACCOUNT_TIMEOUT_S), google.http_client() as client:
            await _check(client, access, outcome)
    except TimeoutError:
        log.warning("mail: check for user %s timed out", user_id)
        await db(store.finish, user_id, account_id, TOO_SLOW)
        outcome.failed += 1
    except Exception:
        # A traceback names code, not values, and no exception text here holds a token
        # (`google.py`, `core/http.py`).
        log.exception("mail: check for user %s failed", user_id)
        await db(store.finish, user_id, account_id, UNEXPECTED)
        outcome.failed += 1


async def check_user(user_id: str) -> Outcome:
    """Check one user's mailbox now (after connecting it)."""
    outcome = Outcome()
    account_id = await db(store.account_of, user_id)
    if account_id is not None:
        await _check_account(user_id, account_id, outcome)
    return outcome


async def check_all() -> Outcome:
    """Check every connected mailbox, the one checked longest ago first. One account's trouble
    is noted on that account and the rest are still checked."""
    outcome = Outcome()
    for user_id, account_id in await db(store.connected_accounts):
        await _check_account(user_id, account_id, outcome)
    return outcome
