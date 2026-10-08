"""Accounts management commands: `dev_session`, `create_test_user`, `set_password` (design §4.6)."""

from __future__ import annotations

from typing import NoReturn

from django.core.management.base import BaseCommand


def fail(command: BaseCommand, message: str) -> NoReturn:
    """Print `message` as is and exit 1. (A `CommandError` would prefix "CommandError: ", and
    these commands keep the TypeScript scripts' exact messages.)"""
    command.stderr.write(message)
    raise SystemExit(1)
