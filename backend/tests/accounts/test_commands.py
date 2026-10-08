"""`create_test_user` and `set_password` (design §4.6)."""

from __future__ import annotations

import io
import re
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from apps.accounts.management.commands import set_password as set_password_command
from apps.accounts.models import User
from apps.accounts.schemas import SAMPLE_PREFERENCES
from apps.llm.models import LlmCall
from apps.matching.models import JobMatch
from apps.resumes.models import Resume
from apps.resumes.queries import latest_base_resume
from tests.factories import make_job, make_llm_call, make_match, make_user, sample_resume_data


def run(command: str, *args: str) -> tuple[list[str], str]:
    out, err = io.StringIO(), io.StringIO()
    call_command(command, *args, stdout=out, stderr=err)
    return out.getvalue().splitlines(), err.getvalue().strip()


def refused(command: str, *args: str) -> str:
    err = io.StringIO()
    with pytest.raises(SystemExit) as exit_info:
        call_command(command, *args, stdout=io.StringIO(), stderr=err)
    assert exit_info.value.code == 1
    return err.getvalue().strip()


# --- create_test_user ----------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_test_user_plain(debug: None) -> None:
    lines, _ = run("create_test_user")
    assert len(lines) == 1
    match = re.fullmatch(r"user: (test-[a-z0-9]{10}@example\.test)", lines[0])
    assert match, lines
    user = User.objects.get(email=match.group(1))
    assert not user.has_usable_password()
    assert user.preferences == {}
    assert latest_base_resume(user.pk) is None
    assert not JobMatch.objects.for_user(user.pk).exists()


@pytest.mark.django_db
def test_create_test_user_with_resume_and_match(debug: None) -> None:
    make_job(posted_at=timezone.now() - timedelta(days=9))
    newest = make_job(posted_at=timezone.now() - timedelta(hours=1))
    make_job(posted_at=timezone.now(), is_active=False)

    lines, _ = run(
        "create_test_user", "--email", "Given@Example.test", "--with-resume", "--with-match"
    )
    assert lines[0] == "user: given@example.test"
    user = User.objects.get(email="given@example.test")
    assert user.preferences == SAMPLE_PREFERENCES.dump()

    base = latest_base_resume(user.pk)
    assert base is not None and base.version == 1
    assert base.resume.dump() == sample_resume_data()

    match = JobMatch.objects.for_user(user.pk).get()
    assert lines[1] == f"match: {match.pk}"
    assert (match.job_id, match.stage) == (newest.pk, "found")

    # Signed in through dev_session, the user can start working at once.
    cookie_lines, _ = run("dev_session", "given@example.test")
    client = Client()
    client.cookies[settings.SESSION_COOKIE_NAME] = cookie_lines[1].rsplit("=", 1)[1]
    assert client.get("/api/me").json()["email"] == "given@example.test"


@pytest.mark.django_db
def test_create_test_user_refusals(debug: None, settings: Any) -> None:
    assert refused("create_test_user", "--with-match") == (
        "The job mirror is empty, so there is no job to match."
    )
    assert not User.objects.exists(), "nothing is written when the mirror is empty"

    make_user(email="taken@example.test")
    assert refused("create_test_user", "--email", "TAKEN@example.test") == (
        "A user with email taken@example.test already exists."
    )
    assert refused("create_test_user", "--purge", "--with-resume") == (
        "--purge takes no other option."
    )

    settings.DEBUG = False
    assert refused("create_test_user") == "create_test_user is for local development only."
    assert (
        refused("create_test_user", "--purge") == "create_test_user is for local development only."
    )


@pytest.fixture
def storage(settings: Any, tmp_path: Path) -> Iterator[Path]:
    root = tmp_path / "storage"
    root.mkdir()
    settings.STORAGE_DIR = root
    yield root


@pytest.mark.django_db
def test_create_test_user_purge(debug: None, storage: Path) -> None:
    keep = make_user(email="real.person@example.com")
    doomed = [make_user(with_resume=True), make_user()]
    make_match(doomed[0])
    ledger = make_llm_call(user=doomed[0])
    for user in (*doomed, keep):
        folder = storage / user.pk / "resume_upload"
        folder.mkdir(parents=True)
        (folder / "cv.pdf").write_bytes(b"%PDF-1.7")
    (storage / "unrelated.txt").write_text("stays")

    lines, _ = run("create_test_user", "--purge")
    assert lines == ["Deleted 2 test users."]
    assert list(User.objects.values_list("email", flat=True)) == ["real.person@example.com"]
    assert not Resume.objects.unscoped("test: purge cascades").exists()
    assert not JobMatch.objects.unscoped("test: purge cascades").exists()
    # The ledger keeps its row, with no user.
    ledger_row = LlmCall.objects.unscoped("test: purge keeps the ledger").get(pk=ledger.pk)
    assert ledger_row.user_id is None
    assert sorted(path.name for path in storage.iterdir()) == [keep.pk, "unrelated.txt"]

    assert run("create_test_user", "--purge")[0] == ["Deleted 0 test users."]
    make_user()
    assert run("create_test_user", "--purge")[0] == ["Deleted 1 test user."]


# --- set_password --------------------------------------------------------------------------------


class Prompts:
    """Stands in for `getpass.getpass`: gives `answers` in order and records what was asked. With
    no answer left it raises `KeyboardInterrupt`, as Ctrl-C at the prompt does."""

    def __init__(self) -> None:
        self.answers: list[str] = []
        self.asked: list[str] = []

    def __call__(self, prompt: str = "") -> str:
        self.asked.append(prompt)
        if not self.answers:
            raise KeyboardInterrupt
        return self.answers.pop(0)


@pytest.fixture
def prompts(monkeypatch: pytest.MonkeyPatch) -> Prompts:
    fake = Prompts()
    monkeypatch.setattr(set_password_command.getpass, "getpass", fake)
    monkeypatch.setattr(set_password_command, "interactive", lambda: True)
    return fake


@pytest.mark.django_db
def test_set_password_updates_and_signs_out_other_sessions(prompts: Prompts) -> None:
    user = make_user(email="forgot@example.test", password="the old password")
    other = Client()
    other.force_login(user)
    assert other.get("/api/me").status_code == 200

    prompts.answers.extend(["a brand new password", "a brand new password"])
    lines, _ = run("set_password", "Forgot@example.test")
    assert lines == ["Password updated. Sign in at http://localhost:3000/sign-in"]
    assert prompts.asked == ["New password for forgot@example.test: ", "Type it again: "]

    user.refresh_from_db()
    assert user.check_password("a brand new password")
    assert user.password.startswith("pbkdf2_sha256$")
    # Django's session auth hash covers the password: every existing session is signed out.
    assert other.get("/api/me").status_code == 401


@pytest.mark.django_db
def test_set_password_messages(prompts: Prompts, monkeypatch: pytest.MonkeyPatch) -> None:
    user = make_user(email="forgot@example.test", password="the old password")
    before = user.password

    assert (
        refused("set_password", "nobody@example.test") == "No user with email nobody@example.test."
    )

    prompts.answers.extend(["short"])
    assert (
        refused("set_password", "forgot@example.test") == "Passwords must be 8 to 128 characters."
    )
    prompts.answers.extend(["x" * 129])
    assert (
        refused("set_password", "forgot@example.test") == "Passwords must be 8 to 128 characters."
    )

    prompts.answers.extend(["first password", "second password"])
    assert refused("set_password", "forgot@example.test") == (
        "Those did not match. Nothing was changed."
    )
    prompts.answers.clear()
    assert refused("set_password", "forgot@example.test") == "Cancelled."

    monkeypatch.setattr(set_password_command, "interactive", lambda: False)
    assert refused("set_password", "forgot@example.test") == (
        "Run this in a terminal: the password is typed at a hidden prompt."
    )
    user.refresh_from_db()
    assert user.password == before
