"""`manage.py create_test_user [--email e] [--with-resume] [--with-match] | --purge` (DEBUG only;
design §4.6).

Throwaway users for checks and browser rehearsals, so nobody types a password into the pane:
create one here, then sign in as it with `dev_session`.

- A new user is `test-<random>@example.test` (or `--email`), with an **unusable** password and
  empty preferences. It prints `user: <email>`.
- `--with-resume` adds the synthetic base resume (`apps/accounts/sample_resume.json`, version 1,
  the same data `tests/factories.py` uses) and complete preferences, so a search can start at once.
- `--with-match` adds one `found` match on the newest active job in the mirror (it fails if the
  mirror is empty) and also prints `match: <id>`, so tailoring and evaluation can be checked
  without a search.
- `--purge` deletes every `@example.test` user, with their rows (cascade) and their
  `STORAGE_DIR/<user_id>/` files, and prints the count. Their ledger rows stay, with no user.
"""

from __future__ import annotations

import json
import secrets
import shutil
import string
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandParser
from django.db import transaction
from django.db.models import F

import apps.accounts
from apps.accounts.management import fail
from apps.accounts.models import User, normalize_email_address
from apps.accounts.schemas import SAMPLE_PREFERENCES
from apps.jobs.models import Job
from apps.matching.models import JobMatch, Stage
from apps.resumes.models import Resume, ResumeKind

TEST_DOMAIN = "@example.test"
SAMPLE_RESUME_PATH = Path(apps.accounts.__file__).resolve().parent / "sample_resume.json"
_RANDOM_ALPHABET = string.ascii_lowercase + string.digits


def random_test_email() -> str:
    return f"test-{''.join(secrets.choice(_RANDOM_ALPHABET) for _ in range(10))}{TEST_DOMAIN}"


def newest_active_job() -> Job | None:
    """The newest active posting, in the order the search prefilter's index reads them."""
    return (
        Job.objects.filter(is_active=True)
        .order_by(F("posted_at").desc(nulls_last=True), "-first_seen_at", "id")
        .first()
    )


def purge_test_users() -> int:
    """Delete every `@example.test` user and their stored files. Returns how many."""
    ids = list(User.objects.filter(email__endswith=TEST_DOMAIN).values_list("pk", flat=True))
    if not ids:
        return 0
    with transaction.atomic():
        User.objects.filter(pk__in=ids).delete()
    root = Path(settings.STORAGE_DIR).resolve()
    for user_id in ids:
        folder = (root / str(user_id)).resolve()
        # Only a direct child of STORAGE_DIR named after the user, never anything else.
        if folder.parent == root and folder.name == str(user_id) and folder.is_dir():
            shutil.rmtree(folder)
    return len(ids)


class Command(BaseCommand):
    help = "Create a throwaway @example.test user, or purge them all (DEBUG only)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--email", help=f"default: test-<random>{TEST_DOMAIN}")
        parser.add_argument(
            "--with-resume",
            action="store_true",
            help="add the synthetic base resume and complete preferences",
        )
        parser.add_argument(
            "--with-match",
            action="store_true",
            help="add one found match on the newest active job",
        )
        parser.add_argument(
            "--purge",
            action="store_true",
            help=f"delete every {TEST_DOMAIN} user and their files",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if not settings.DEBUG:
            fail(self, "create_test_user is for local development only.")
        email: str | None = options.get("email")
        with_resume: bool = options.get("with_resume", False)
        with_match: bool = options.get("with_match", False)

        if options.get("purge"):
            if email or with_resume or with_match:
                fail(self, "--purge takes no other option.")
            count = purge_test_users()
            self.stdout.write(f"Deleted {count} test {'user' if count == 1 else 'users'}.")
            return

        address = normalize_email_address(email) if email else random_test_email()
        if User.objects.filter(email=address).exists():
            fail(self, f"A user with email {address} already exists.")
        job = None
        if with_match:
            job = newest_active_job()
            if job is None:
                fail(self, "The job mirror is empty, so there is no job to match.")

        with transaction.atomic():
            user = User.objects.create_user(
                address,
                password=None,  # unusable: this user signs in only through dev_session
                name="Test User",
                preferences=SAMPLE_PREFERENCES.dump() if with_resume else {},
            )
            if with_resume:
                Resume.objects.create_for(
                    user.pk,
                    kind=ResumeKind.BASE,
                    version=1,
                    data=json.loads(SAMPLE_RESUME_PATH.read_text()),
                )
            match = (
                JobMatch.objects.create_for(user.pk, job=job, stage=Stage.FOUND)
                if job is not None
                else None
            )

        self.stdout.write(f"user: {user.email}")
        if match is not None:
            self.stdout.write(f"match: {match.pk}")
