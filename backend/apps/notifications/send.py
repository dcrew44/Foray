"""Sending what is due (#59). Async: the outbox's sync steps run through `db()`, and the
request to ntfy happens between them, outside any transaction.

`send_due(user_id)` is one user's notifications, right after a watch queued them;
`send_due()` is everyone's, for the sweep that runs every minute and picks up what quiet hours
held and what failed before. One user's trouble is logged and the rest are still sent.
`send_trial(user_id)` is Settings' test notification, which touches no row.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from apps.core.db import db
from apps.core.http import HttpClient
from apps.notifications import compose, ntfy, outbox

__all__ = ["TRIAL_SENT", "Outcome", "Trial", "send_due", "send_trial"]

log = logging.getLogger("foray.notifications")

UNEXPECTED = "Sending failed unexpectedly; the worker's log has the details."
TRIAL_SENT = "Sent. It should reach your phone in a few seconds."


@dataclass(slots=True)
class Outcome:
    sent: int = 0  # messages the channel accepted
    failed: int = 0  # messages it did not


@dataclass(frozen=True, slots=True)
class Trial:
    """How Settings' test notification went, in the sentence the page shows."""

    sent: bool
    message: str


async def _send_batch(client: HttpClient, batch: outbox.Batch, outcome: Outcome) -> None:
    for delivery in batch.deliveries:
        try:
            await ntfy.publish(client, batch.topic, delivery.message)
        except ntfy.PublishError as error:
            reason = str(error)
        except Exception:
            log.exception("notify: a notification for user %s failed to send", batch.user_id)
            reason = UNEXPECTED
        else:
            await db(outbox.record_sent, batch.user_id, delivery.ids)
            outcome.sent += 1
            continue
        await db(outbox.record_failure, batch.user_id, delivery.ids, reason)
        outcome.failed += 1
        log.warning("notify: not sent for user %s: %s", batch.user_id, reason)


async def send_due(user_id: str | None = None) -> Outcome:
    """Send every due notification of one user, or of all of them."""
    user_ids = [user_id] if user_id else await db(outbox.due_user_ids)
    outcome = Outcome()
    client: HttpClient | None = None
    try:
        for uid in user_ids:
            try:
                batch = await db(outbox.claim, uid)
                if batch is None:
                    continue
                if client is None:
                    client = ntfy.http_client()
                await _send_batch(client, batch, outcome)
            except Exception:
                log.exception("notify: could not send user %s's notifications", uid)
    finally:
        if client is not None:
            await client.aclose()
    return outcome


async def send_trial(user_id: str) -> Trial:
    """Settings' test: one message to the user's saved topic, now, whatever the hour (they
    asked for it). Never raises for a send that failed: the result says why."""
    topic = await db(outbox.topic_of, user_id)
    if not topic:
        return Trial(False, outbox.NO_TOPIC)
    try:
        async with ntfy.http_client() as client:
            await ntfy.publish(client, topic, compose.trial_message())
    except ntfy.PublishError as error:
        return Trial(False, str(error))
    except Exception:
        log.exception("notify: the test notification for user %s failed to send", user_id)
        return Trial(False, UNEXPECTED)
    return Trial(True, TRIAL_SENT)
