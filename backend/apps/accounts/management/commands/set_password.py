"""`pnpm auth:set-password <email>`: set a forgotten password (design §4.6).

There is no mail transport, so there is no emailed reset link. The password is typed twice at a
hidden prompt, never passed as an argument, so it stays out of shell history. Changing it signs
out every existing session (Django's session auth hash covers the password); the TypeScript script
kept them.
"""

from __future__ import annotations

import getpass
import sys
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandParser

from apps.accounts.hashers import PASSWORD_MAX, PASSWORD_MIN, js_length
from apps.accounts.management import fail
from apps.accounts.models import User, normalize_email_address


def interactive() -> bool:
    return sys.stdin.isatty()


class Command(BaseCommand):
    help = "Set a user's password at a hidden prompt."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("email")

    def handle(self, *args: Any, email: str, **options: Any) -> None:
        user = User.objects.filter(email=normalize_email_address(email)).first()
        if user is None:
            fail(self, f"No user with email {email}.")
        if not interactive():
            fail(self, "Run this in a terminal: the password is typed at a hidden prompt.")
        try:
            password = getpass.getpass(f"New password for {user.email}: ")
            if not PASSWORD_MIN <= js_length(password) <= PASSWORD_MAX:
                fail(self, f"Passwords must be {PASSWORD_MIN} to {PASSWORD_MAX} characters.")
            again = getpass.getpass("Type it again: ")
        except KeyboardInterrupt, EOFError:
            fail(self, "Cancelled.")
        if again != password:
            fail(self, "Those did not match. Nothing was changed.")

        user.set_password(password)
        user.save(update_fields=["password"])
        self.stdout.write(f"Password updated. Sign in at {settings.PUBLIC_ORIGIN}/sign-in")
