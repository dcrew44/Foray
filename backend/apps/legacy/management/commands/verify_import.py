"""`pnpm db:verify-import [--password-prompt]`: re-run the import's checks (design §3.2, §3.4).

With `--password-prompt` the **user** then types their password at a hidden prompt. It is checked
against the imported Better Auth hash with `BetterAuthScryptHasher.verify`; nothing is saved or
upgraded.
"""

from __future__ import annotations

import getpass
import sys
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.accounts.hashers import ALGORITHM, BetterAuthScryptHasher
from apps.accounts.models import User, normalize_email_address
from apps.legacy.importer import ImportFailure, ImportRefused, Options, run_import
from apps.legacy.management.commands.import_legacy import print_outcome

MATCHES = "Password matches the imported hash."
DOES_NOT_MATCH = "Password does not match."


def interactive() -> bool:
    return sys.stdin.isatty()


class Command(BaseCommand):
    help = "Re-run the legacy import's checks; optionally check a password against its hash."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--password-prompt",
            action="store_true",
            help="then check a password typed at a hidden prompt against the imported hash",
        )
        parser.add_argument("--email", help="whose password (default: the one imported user)")

    def handle(self, *args: Any, password_prompt: bool, email: str | None, **options: Any) -> None:
        try:
            outcome = run_import(Options(verify_only=True), out=self.stdout.write)
        except (ImportRefused, ImportFailure) as exc:
            raise CommandError(f"Refused: {exc}") from None
        print_outcome(self, outcome)
        if not (outcome.verification.ok and outcome.untouched.ok):
            raise CommandError("Verification failed.")
        self.stdout.write(f"Verification passed in {outcome.seconds:.1f} s.")
        if password_prompt:
            self.check_password(email)

    def check_password(self, email: str | None) -> None:
        imported = User.objects.filter(password__startswith=f"{ALGORITHM}$")
        if email is not None:
            user = User.objects.filter(email=normalize_email_address(email)).first()
            if user is None:
                raise CommandError(f"No user with email {email}.")
            if not user.password.startswith(f"{ALGORITHM}$"):
                raise CommandError(
                    f"{user.email} no longer has the imported hash (a sign-in replaced it, or "
                    "the account had no password)."
                )
        else:
            users = list(imported[:2])
            if not users:
                raise CommandError("No user still has an imported password hash.")
            if len(users) > 1:
                raise CommandError("Several users have imported hashes; pass --email.")
            user = users[0]
        if not interactive():
            raise CommandError("Run this in a terminal: the password is typed at a hidden prompt.")
        try:
            password = getpass.getpass(f"Password for {user.email}: ")
        except KeyboardInterrupt, EOFError:
            raise CommandError("Cancelled.") from None
        if BetterAuthScryptHasher().verify(password, user.password):
            self.stdout.write(MATCHES)
        else:
            self.stdout.write(DOES_NOT_MATCH)
            raise SystemExit(1)
