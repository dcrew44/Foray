"""Shared test setup (design §1.3, §12.1).

- Settings are `foray.settings_test` (pyproject's `DJANGO_SETTINGS_MODULE`): no test reads `.env`
  or the shell's variables, and pytest-django builds its own test database from migrations.
- Outbound network is blocked for the whole session: a connect to anything but loopback or a
  Unix socket raises `NetworkBlocked`. Postgres and the Node contract subprocess are local;
  model providers, JSearch and Brave are not, so a test that forgets a fake fails instead of
  spending.
- Transactional database tests start and end with procrastinate's tables empty: its models are
  unmanaged, so the flush between transactional tests does not reach them.

A lane's own fixtures go in `tests/<app>/conftest.py`, never here.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterator
from typing import Any

import pytest


class NetworkBlocked(ConnectionRefusedError):
    """Raised for an outbound connection in tests. Use a fake instead."""


def _is_local(sock: socket.socket, address: Any) -> bool:
    if getattr(socket, "AF_UNIX", None) is not None and sock.family == socket.AF_UNIX:
        return True
    if not isinstance(address, tuple) or not address:
        return False
    host = str(address[0]).split("%", 1)[0]  # drop an IPv6 zone id
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False  # a name other than localhost would need DNS: not local


_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _guarded_connect(self: socket.socket, address: Any) -> None:
    if not _is_local(self, address):
        raise NetworkBlocked(f"Outbound network is blocked in tests (connect to {address!r}).")
    return _real_connect(self, address)


def _guarded_connect_ex(self: socket.socket, address: Any) -> int:
    if not _is_local(self, address):
        raise NetworkBlocked(f"Outbound network is blocked in tests (connect to {address!r}).")
    return _real_connect_ex(self, address)


@pytest.fixture(autouse=True, scope="session")
def block_outbound_network() -> Iterator[None]:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(socket.socket, "connect", _guarded_connect)
        mp.setattr(socket.socket, "connect_ex", _guarded_connect_ex)
        yield


@pytest.fixture(autouse=True)
def _empty_queue_around_transactional_tests(request: pytest.FixtureRequest) -> Iterator[None]:
    marker = request.node.get_closest_marker("django_db")
    if marker is None or not (marker.kwargs.get("transaction") or (marker.args[:1] == (True,))):
        yield
        return
    from tests.queue import clear_queue

    request.getfixturevalue("transactional_db")
    clear_queue()
    yield
    clear_queue()
