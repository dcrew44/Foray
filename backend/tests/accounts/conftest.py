"""Fixtures for the accounts tests.

Every unsafe request carries `Origin: http://localhost:3000`, as a browser does through the Next
rewrite; without it, a request that carries the session cookie is refused by the Origin check
(§4.3). The `api` fixture wraps Django's test client with that default (`helpers.Api`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from django.test import Client

from apps.accounts.models import User
from tests.accounts.helpers import Api
from tests.factories import make_user


@pytest.fixture
def api(client: Client) -> Api:
    return Api(client)


@pytest.fixture
def signed_in(db: None) -> Callable[..., tuple[Api, User]]:
    """`signed_in(**user_fields)`: a fresh client signed in as a new user."""

    def make(**fields: Any) -> tuple[Api, User]:
        user = make_user(**fields)
        client = Client()
        client.force_login(user)
        return Api(client), user

    return make


@pytest.fixture
def debug(settings: Any) -> None:
    """The dev-only commands refuse unless DEBUG (pytest-django runs tests with DEBUG off)."""
    settings.DEBUG = True
