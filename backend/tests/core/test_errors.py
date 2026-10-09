"""A body ninja cannot parse gets the operation's own sentence (design §5.2).

ninja raises `HttpError(400, "Cannot parse request body")` before validation (with the parser's
own error appended under DEBUG). That is unreadable input like any other: it answers the
operation's `invalid` message, or the default sentence when the operation names none, and the
library's text never reaches the client.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from django.test import Client

from apps.accounts.api import PREFERENCES_INVALID
from apps.core.errors import DEFAULT_INVALID
from apps.matching.api import CHANGE_UNREADABLE
from apps.runs.api import ANSWER_UNREADABLE
from tests.factories import make_approval, make_match, make_run, make_user

ORIGIN = "http://localhost:3000"
NOT_JSON = "this is {not json"


def send(client: Client, method: str, path: str, body: str) -> Any:
    return client.generic(
        method, path, data=body, content_type="application/json", headers={"Origin": ORIGIN}
    )


@pytest.fixture
def client() -> tuple[Client, Any]:
    user = make_user(with_resume=True)
    signed_in = Client()
    signed_in.force_login(user)
    return signed_in, user


@pytest.mark.django_db
@pytest.mark.parametrize("debug", [False, True])
def test_a_body_that_is_not_json_gets_the_operations_sentence(
    client: tuple[Client, Any], settings: Any, debug: bool
) -> None:
    settings.DEBUG = debug  # under DEBUG ninja appends the parser's own error to its text
    browser, user = client
    run = make_run(user, status="awaiting_approval")
    approval = make_approval(user, run=run)
    match = make_match(user)
    cases = [
        ("PUT", "/api/preferences", PREFERENCES_INVALID),  # a callable `invalid`
        ("POST", "/api/searches", DEFAULT_INVALID),  # no `@messages`: the default sentence
        ("POST", f"/api/approvals/{approval.id}/decision", ANSWER_UNREADABLE),
        ("PATCH", f"/api/matches/{match.id}", CHANGE_UNREADABLE),
    ]
    for method, path, sentence in cases:
        response = send(browser, method, path, NOT_JSON)
        assert response.status_code == 400, (path, response.content)
        assert response.json() == {"ok": False, "message": sentence}, path
        assert b"parse" not in response.content.lower(), path


@pytest.mark.django_db
def test_a_readable_body_still_validates_field_by_field(client: tuple[Client, Any]) -> None:
    """The parse sentence does not swallow the field-level ones."""
    browser, _ = client
    response = send(browser, "PUT", "/api/preferences", '{"target_roles": "not a list"}')
    assert response.status_code == 400
    assert response.json()["message"] == "Target roles could not be saved: it should be a list."
    gone = send(browser, "PATCH", f"/api/matches/{uuid.uuid4()}", NOT_JSON)
    assert gone.status_code == 400  # the body is read before the match is looked up
