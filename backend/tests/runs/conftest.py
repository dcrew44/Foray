"""Fixtures for the runs tests (design §12.1).

The runs tests use the real job table: every defer goes through `defer_in_tx` on Django's
connection, and the tests that run jobs drain them with a real procrastinate Worker. Tests that
need another session to see their rows, or a Worker's threads to, are
`django_db(transaction=True)`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from django.test import Client

from apps.accounts.models import User
from apps.core.queue import app
from tests.factories import make_user
from tests.queue import worker_connector
from tests.runs import support


@pytest.fixture(autouse=True)
def _clear_calls() -> Iterator[None]:
    support.CALLS.clear()
    yield
    support.CALLS.clear()


@pytest.fixture
def under_worker_connector() -> Iterator[None]:
    """What `manage.py worker` does first: swap in the `PsycopgConnector`. `defer_in_tx` must
    still join the caller's transaction (§9.1)."""
    with app.replace_connector(worker_connector()):
        yield


ORIGIN = "http://localhost:3000"


class Api:
    """`Client` with JSON bodies and the browser's Origin header on unsafe methods (§4.3)."""

    def __init__(self, client: Client) -> None:
        self.client = client

    def get(self, path: str) -> Any:
        return self.client.get(path)

    def post(self, path: str, body: Any = None) -> Any:
        payload = body if isinstance(body, str) else json.dumps({} if body is None else body)
        return self.client.post(
            path, data=payload, content_type="application/json", headers={"Origin": ORIGIN}
        )


@pytest.fixture
def signed_in() -> Callable[..., tuple[Api, User]]:
    """`signed_in(**user_fields)`: a client signed in as a new user (the test's `django_db`
    mark gives the database)."""

    def make(**fields: Any) -> tuple[Api, User]:
        user = make_user(**fields)
        client = Client()
        client.force_login(user)
        return Api(client), user

    return make
