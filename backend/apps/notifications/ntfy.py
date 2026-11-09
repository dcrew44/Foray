"""ntfy, the one channel (#59): publish a message to a topic, and say why when it fails.

A phone subscribes to a topic in the ntfy app; Foray publishes to it with ntfy's JSON API,
`POST {NTFY_SERVER}/` with `{topic, title, message, click, tags}`. The server comes from the
environment and never from a user, so the only user-supplied part of a request is the topic,
which is a field of the body and has no way to name another host. Requests go through the
guarded `HttpClient` like every other: a self-hosted ntfy has to resolve to a public address,
on port 80 or 443.

One attempt per call (`retries=0`). `HttpClient.post_json` retries like a GET, which is right
for a query and wrong here: a retry after an answer that was lost would deliver twice. The
outbox decides about another attempt (`outbox.record_failure`).

A `2xx` is taken as "published" only when the body is the message ntfy stored (it has an
`id`): the client follows a redirect to another origin with a plain GET, and a server that
sent one (`http://` to `https://`, say) would otherwise look like a success with nothing sent.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings

from apps.core.http import FetchError, HttpClient, HttpError, NetworkError
from apps.notifications.compose import Message, one_line

__all__ = ["PublishError", "http_client", "payload", "publish", "server_name"]

SEND_TIMEOUT_S = 10.0
ANSWER_MAX_BYTES = 64 * 1024
REASON_MAX = 160


class PublishError(Exception):
    """A notification that was not published. Its text is one sentence for the user."""


def http_client() -> HttpClient:
    """One guarded client per job. Tests replace this to serve canned answers."""
    return HttpClient()


def server_name() -> str:
    """The configured server's host, for sentences: `ntfy.sh`."""
    return urlsplit(settings.ENV.ntfy_server).hostname or "the ntfy server"


def publish_url() -> str:
    return f"{settings.ENV.ntfy_server}/"


def payload(topic: str, message: Message) -> dict[str, Any]:
    body: dict[str, Any] = {
        "topic": topic,
        "title": message.title,
        "message": message.body,
        "click": message.click,
    }
    if message.tags:
        body["tags"] = list(message.tags)
    return body


def _ntfy_reason(body: str) -> str | None:
    """ntfy's own words for a refusal (`{"code": 42901, "error": "limit reached: ..."}`)."""
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    reason = parsed.get("error") if isinstance(parsed, dict) else None
    return one_line(reason, REASON_MAX) if isinstance(reason, str) and reason.strip() else None


def _refusal(error: FetchError) -> str:
    name = server_name()
    if isinstance(error, NetworkError):
        return f"Could not reach {name}. Check the connection and try again."
    if not isinstance(error, HttpError):
        return f"The notification could not be sent to {name}."
    if error.blocked:
        return (
            f"Foray will not connect to {name}: it resolves to a private address. A self-hosted "
            "ntfy server has to be reachable from the internet."
        )
    if error.status == 429:
        return f"{name} is limiting how many notifications it accepts. Try again in a while."
    if error.status in (401, 403):
        return f"{name} refused this topic: it is reserved or needs a sign-in. Pick another name."
    if isinstance(error.status, int):
        reason = _ntfy_reason(error.body)
        return f"{name} answered {error.status}" + (f": {reason}." if reason else ".")
    return f"{name} gave an answer Foray could not use."


async def publish(client: HttpClient, topic: str, message: Message) -> None:
    """Publish one message. Raises `PublishError` with the sentence to show or record."""
    try:
        answer = await client.post_json(
            publish_url(),
            payload(topic, message),
            timeout=SEND_TIMEOUT_S,
            retries=0,
            max_bytes=ANSWER_MAX_BYTES,
        )
    except FetchError as error:
        raise PublishError(_refusal(error)) from error
    try:
        stored = json.loads(answer)
    except ValueError:
        stored = None
    if not isinstance(stored, dict) or not stored.get("id"):
        raise PublishError(
            f"{server_name()} answered, but not as an ntfy server does. Check NTFY_SERVER."
        )
