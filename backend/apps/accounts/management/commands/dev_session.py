"""`pnpm dev:session [email]`: mint a real session for a local user (DEBUG only; design §4.6).

The user is the one with that email (lowercased), else the **oldest** user. The output keeps the
shape CLAUDE.md's browser-pane recipe reads:

    user: you@example.com
    cookie: sessionid=<key>

Drop the `cookie: ` prefix and set the rest with `document.cookie`. A session set that way is not
`HttpOnly`; it carries no `_seen` stamp, so its first browser request slides it and the browser
gets a proper cookie (`foray.middleware.sliding_session`).
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.core.management.base import BaseCommand, CommandParser

from apps.accounts.management import fail
from apps.accounts.models import User, normalize_email_address

MODEL_BACKEND = "django.contrib.auth.backends.ModelBackend"


class Command(BaseCommand):
    help = "Print a session cookie for a local user (DEBUG only)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("email", nargs="?", help="the user's email; default: the oldest user")

    def handle(self, *args: Any, email: str | None = None, **options: Any) -> None:
        if not settings.DEBUG:
            fail(self, "dev_session is for local development only.")
        if email:
            user = User.objects.filter(email=normalize_email_address(email)).first()
            if user is None:
                fail(self, f"No user with email {email}.")
        else:
            user = User.objects.order_by("created_at", "pk").first()
            if user is None:
                fail(self, "No users yet.")

        session = import_module(settings.SESSION_ENGINE).SessionStore()
        session[SESSION_KEY] = str(user.pk)  # what login() stores for a text primary key
        session[BACKEND_SESSION_KEY] = MODEL_BACKEND
        session[HASH_SESSION_KEY] = user.get_session_auth_hash()
        session.create()

        self.stdout.write(f"user: {user.email}")
        self.stdout.write(f"cookie: {settings.SESSION_COOKIE_NAME}={session.session_key}")
