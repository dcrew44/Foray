"""Reading a mailbox (#26): what a check asks Gmail for, what it keeps, and what it changes.

The mailbox is the fake of `conftest.py`. A check runs as the worker runs it
(`check.check_user`, or the `mail.check` task on a real in-process Worker); its database steps
run on `db()` threads, which see only committed rows, so these tests are transactional.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models.functions import Now
from django.test import Client
from django.utils import timezone
from procrastinate.periodic import PeriodicDeferrer, PeriodicRegistry
from procrastinate.retry import RetryStrategy

from apps.accounts.models import User
from apps.applications import actions
from apps.applications.models import Application
from apps.core.queue import defer_in_tx
from apps.mail import check, connect, crypto, google, store
from apps.mail.check import read_message
from apps.mail.models import ApplicationEmail, MailAccount
from apps.mail.tasks import mail_check
from apps.matching.models import JobMatch
from apps.runs import periodic
from apps.runs.step import TASKS
from tests.accounts.helpers import Api
from tests.factories import (
    make_application,
    make_application_email,
    make_company,
    make_job,
    make_match,
    make_user,
)
from tests.mail.conftest import MAILBOX, FakeGoogle, days_ago, secrets_of
from tests.queue import run_deferred
from tests.runs.support import jobs

pytestmark = pytest.mark.django_db(transaction=True)

SSR = {"X-Foray-SSR": "1"}
RELAY = "no-reply@greenhouse-mail.io"


def applied(
    user: User,
    company: str,
    title: str = "Software Engineer",
    *,
    sent: float = 2,
    domain: str | None = None,
    by_hand: bool = False,
    stage: str = "applied",
) -> JobMatch:
    """A match the user applied to `sent` days ago: through Foray (an application marked
    sent), or by hand (only the stage says so)."""
    at = days_ago(sent)
    job = make_job(company=make_company(name=company, domain=domain), title=title)
    match = make_match(user, job=job, stage=stage, stage_changed_at=at)
    if not by_hand:
        make_application(user, match, status="submitted", submitted_at=at)
    return match


def run_check(user: User) -> check.Outcome:
    return asyncio.run(check.check_user(user.pk))


def emails(user: User) -> list[ApplicationEmail]:
    return list(ApplicationEmail.objects.for_user(user.pk).order_by("received_at", "id"))


def by_subject(user: User) -> dict[str, ApplicationEmail]:
    return {row.subject: row for row in emails(user)}


def account(user: User) -> MailAccount:
    return MailAccount.objects.for_user(user.pk).get()


def application_of(match: JobMatch) -> Application:
    return Application.objects.for_user(match.user_id).get(match_id=match.id)


def stage_of(match: JobMatch) -> str:
    return JobMatch.objects.for_user(match.user_id).get(id=match.id).stage


def confirmation(server: FakeGoogle, company: str, **fields: Any) -> str:
    return server.deliver(
        sender=f"{company} Recruiting <{RELAY}>",
        subject=f"Thank you for applying to {company}",
        snippet=f"Hi Sam, we received your application to {company} and will review it shortly.",
        **fields,
    )


def rejection(server: FakeGoogle, company: str, **fields: Any) -> str:
    return server.deliver(
        sender=f"{company} Recruiting <{RELAY}>",
        subject=f"Your application to {company}",
        snippet="Hi Sam, thank you for applying. Unfortunately, we have decided to move "
        "forward with other candidates.",
        **fields,
    )


def invitation(server: FakeGoogle, company: str, **fields: Any) -> str:
    return server.deliver(
        sender=f"{company} Recruiting <{RELAY}>",
        subject=f"Interview with {company}",
        snippet="Hi Sam, we would like to schedule a 30-minute phone screen. Please share "
        "your availability for next week.",
        **fields,
    )


def newsletter(server: FakeGoogle, **fields: Any) -> str:
    return server.deliver(
        sender="Jobhop <alerts@jobhop.example>",
        subject="12 new backend jobs for you",
        snippet="Sam, apply now to roles picked for you: Backend Engineer at Acme and 11 more.",
        **fields,
    )


# --- what a check asks for -----------------------------------------------------------------------


def test_a_check_reads_only_the_metadata_of_messages_it_has_not_seen(
    google_server: FakeGoogle,
) -> None:
    user = make_user()
    applied(user, "Acme")
    google_server.connect(user)
    first = [newsletter(google_server), confirmation(google_server, "Acme")]

    outcome = run_check(user)

    assert (outcome.checked, outcome.stored, outcome.matched, outcome.failed) == (1, 2, 1, 0)
    assert sorted(google_server.reads()) == sorted(first)
    # Nothing but GETs went to Gmail, with the stored access token and no refresh.
    assert {request.method for request in google_server.gmail_requests} == {"GET"}
    assert google_server.token_requests == [] and google_server.refused == []
    listing, *gets = google_server.gmail_requests
    assert listing.url.path == "/gmail/v1/users/me/messages"
    assert listing.url.params["q"] == google.QUERY
    assert "newer_than:14d" in google.QUERY and "-category:promotions" in google.QUERY
    for request in gets:
        params = request.url.params
        # Metadata only: the three headers, and no format that carries a body.
        assert params["format"] == "metadata"
        assert sorted(params.get_list("metadataHeaders")) == ["Date", "From", "Subject"]
        assert request.content == b""
    assert account(user).last_checked_at is not None and account(user).last_error == ""

    # Later: one new message. Only it is read.
    google_server.requests.clear()
    new = rejection(google_server, "Acme")
    outcome = run_check(user)
    assert google_server.reads() == [new] and outcome.stored == 1
    # And with nothing new, a check is one listing.
    google_server.requests.clear()
    assert run_check(user).stored == 0
    assert [request.url.path for request in google_server.requests] == [
        "/gmail/v1/users/me/messages"
    ]
    assert len(emails(user)) == 3


def test_what_is_kept_of_a_message_is_the_minimum(google_server: FakeGoogle) -> None:
    user = make_user()
    applied(user, "Acme")
    google_server.connect(user)
    at = days_ago(1).replace(microsecond=0)
    long_snippet = "Hi Sam, we received your application. " + "It is being reviewed. " * 30
    kept_id = google_server.deliver(
        sender='"Acme Recruiting" <jo.recruiter@greenhouse-mail.io>',
        subject="Thank you for applying to Acme",
        snippet=long_snippet,
        at=at,
    )
    other_id = newsletter(google_server)

    run_check(user)

    rows = {row.message_id: row for row in emails(user)}
    kept, other = rows[kept_id], rows[other_id]
    assert (kept.kind, kept.sender_name, kept.sender_domain) == (
        "confirmation",
        "Acme Recruiting",
        "greenhouse-mail.io",
    )
    assert kept.subject == "Thank you for applying to Acme" and kept.received_at == at
    assert kept.snippet == long_snippet.strip()[:300] and len(kept.snippet) == 300
    # A message that is not an employer's answer keeps only what stops it being read again.
    assert (other.kind, other.sender_name, other.sender_domain) == ("other", "", "")
    assert (other.subject, other.snippet, other.match_id) == ("", "", None)
    # Nowhere: the sender's address, the recipient, or a body.
    stored = " ".join(
        f"{r.sender_name} {r.sender_domain} {r.subject} {r.snippet}" for r in rows.values()
    )
    assert "jo.recruiter" not in stored and MAILBOX not in stored
    assert "VGhlIGZ1bGwgdGV4dCBvZiB0aGUgbWVzc2FnZS4=" not in stored


def test_the_table_refuses_content_for_mail_that_is_not_about_an_application() -> None:
    user = make_user()
    with (
        pytest.raises(IntegrityError, match="application_emails_other_is_blank"),
        transaction.atomic(),
    ):
        make_application_email(user, kind="other", subject="A newsletter's subject")
    with (
        pytest.raises(IntegrityError, match="application_emails_snippet_short"),
        transaction.atomic(),
    ):
        make_application_email(user, snippet="x" * 301)
    with (
        pytest.raises(IntegrityError, match="application_emails_decision_paired"),
        transaction.atomic(),
    ):
        make_application_email(user, decision="accepted", decided_at=Now())  # not a suggestion
    row = make_application_email(user, message_id="m-1")
    with (
        pytest.raises(IntegrityError, match="application_emails_once_per_message"),
        transaction.atomic(),
    ):
        make_application_email(user, account=row.account, message_id="m-1")
    with pytest.raises(IntegrityError), transaction.atomic():
        MailAccount.objects.create_for(user.pk, address="b@mailbox.example", refresh_token="v1:x")


def test_read_message_takes_the_name_and_domain_and_drops_the_rest() -> None:
    def message(sender: str, subject: str = "Hello", **fields: Any) -> dict[str, Any]:
        headers = [{"name": "From", "value": sender}, {"name": "Subject", "value": subject}]
        return {"id": "m1", "internalDate": "1794000000000", "snippet": "", **fields} | {
            "payload": {"headers": headers + fields.get("headers", [])}
        }

    read = read_message(message("Acme Recruiting <Jo.Recruiter@Mail.Acme.Example>"))
    assert read is not None
    assert (read.sender_name, read.sender_domain) == ("Acme Recruiting", "mail.acme.example")
    assert read.received_at == datetime.fromtimestamp(1_794_000_000, tz=UTC)
    # A bare address has no display name; an encoded name is decoded; a missing one is "".
    assert read_message(message("jo@acme.example")).sender_name == ""  # type: ignore[union-attr]
    encoded = read_message(message("=?UTF-8?B?QWNtw6kgUmVjcnVpdGluZw==?= <jo@acme.example>"))
    assert encoded is not None and encoded.sender_name == "Acmé Recruiting"
    quoted = read_message(message('"Acme, Inc. Hiring" <jo@acme.example>'))
    assert quoted is not None and quoted.sender_name == "Acme, Inc. Hiring"
    empty = read_message({"id": "m2", "payload": {}})
    assert empty is not None
    assert (empty.sender_name, empty.sender_domain, empty.subject, empty.snippet) == (
        "",
        "",
        "",
        "",
    )
    # Gmail's snippet is HTML-escaped; subjects come on one line.
    escaped = read_message(
        message(
            "a@b.example", "Re:  your\r\n application", snippet="We&#39;ve received it &amp; more"
        )
    )
    assert escaped is not None
    assert (escaped.subject, escaped.snippet) == (
        "Re: your application",
        "We've received it & more",
    )
    # No `internalDate`: the Date header, else now.
    dated = read_message(
        {
            "id": "m3",
            "payload": {"headers": [{"name": "Date", "value": "Mon, 09 Nov 2026 10:00:00 -0500"}]},
        }
    )
    assert dated is not None and dated.received_at == datetime(2026, 11, 9, 15, tzinfo=UTC)
    undated = read_message({"id": "m4"})
    assert undated is not None and abs(undated.received_at - timezone.now()) < timedelta(minutes=1)
    assert read_message({"snippet": "no id"}) is None


# --- proof of arrival ----------------------------------------------------------------------------


def test_a_confirmation_is_recorded_as_proof_on_the_right_application(
    google_server: FakeGoogle,
) -> None:
    user, other = make_user(), make_user()
    acme, globex = applied(user, "Acme"), applied(user, "Globex Systems")
    theirs = applied(other, "Acme")
    google_server.connect(user)
    at = days_ago(1.9).replace(microsecond=0)
    confirmation(google_server, "Acme", at=at)

    outcome = run_check(user)

    assert (outcome.stored, outcome.matched) == (1, 1)
    [row] = emails(user)
    assert (row.kind, row.match_id, row.decision) == ("confirmation", acme.id, None)
    proof = application_of(acme)
    assert (proof.confirmed_at, proof.confirmation_subject) == (
        at,
        "Thank you for applying to Acme",
    )
    # No other application, of this user's or anyone's, and no stage.
    assert application_of(globex).confirmed_at is None
    assert application_of(theirs).confirmed_at is None
    assert (stage_of(acme), stage_of(globex)) == ("applied", "applied")

    # The match page shows it, from the application's own columns.
    page = match_page(user, acme)
    assert page["mail"] == {
        "confirmedAt": at.isoformat().replace("+00:00", "Z"),
        "confirmationSubject": "Thank you for applying to Acme",
        "suggestion": None,
    }
    assert match_page(user, globex)["mail"]["confirmedAt"] is None

    # A second confirmation (a reminder, a duplicate) leaves the first one's date.
    confirmation(google_server, "Acme", at=at + timedelta(hours=5))
    run_check(user)
    assert application_of(acme).confirmed_at == at


def test_a_confirmation_counts_only_as_the_employers_own_word_and_in_time(
    google_server: FakeGoogle,
) -> None:
    user = make_user()
    acme = applied(user, "Acme", sent=20)
    globex = applied(user, "Globex Systems", sent=2)
    google_server.connect(user)
    # A job board saying it passed the application on is not the employer saying it arrived.
    google_server.deliver(
        sender="CareerNest <no-reply@careernest.example>",
        subject="Your application was sent to Globex Systems",
        snippet="Your application was sent to Globex Systems. Backend Engineer. Applied today.",
    )
    # A "we received your application" three weeks after this one was sent is some other
    # application's.
    confirmation(google_server, "Acme")

    assert run_check(user).matched == 0

    assert {row.kind for row in emails(user)} == {"confirmation"}
    assert all(row.match_id is None for row in emails(user))
    assert application_of(acme).confirmed_at is None
    assert application_of(globex).confirmed_at is None
    # A rejection is only a suggestion, so both would be good enough for one of those.
    rejection(google_server, "Acme")
    assert run_check(user).matched == 1
    assert by_subject(user)["Your application to Acme"].match_id == acme.id


def test_an_answer_that_came_before_the_user_said_sent_is_matched_afterwards(
    google_server: FakeGoogle,
) -> None:
    """The usual order: apply on the employer's site, get the confirmation within the minute,
    and only then come back to Foray to say it was sent."""
    user = make_user()
    job = make_job(company=make_company(name="Acme"))
    match = make_match(user, job=job, stage="shortlisted")
    application = make_application(user, match, status="ready")
    google_server.connect(user)
    at = (timezone.now() - timedelta(hours=3)).replace(microsecond=0)
    confirmation(google_server, "Acme", at=at)

    assert run_check(user).matched == 0  # nothing is sent yet, as far as Foray knows
    [row] = emails(user)
    assert (row.kind, row.match_id) == ("confirmation", None)

    assert actions.mark_submitted(user.pk, application.id)
    outcome = run_check(user)

    assert (outcome.stored, outcome.matched) == (0, 1)
    assert google_server.reads() == [row.message_id]  # read once, matched from what was kept
    [row] = emails(user)
    assert row.match_id == match.id
    assert application_of(match).confirmed_at == at


def test_a_match_applied_to_by_hand_shows_its_confirmation_too(google_server: FakeGoogle) -> None:
    user = make_user()
    match = applied(user, "Acme", by_hand=True)
    google_server.connect(user)
    at = days_ago(1.5).replace(microsecond=0)
    confirmation(google_server, "Acme", at=at)

    assert run_check(user).matched == 1

    assert not Application.objects.for_user(user.pk).exists()
    mail = match_page(user, match)["mail"]
    assert mail["confirmationSubject"] == "Thank you for applying to Acme"
    assert mail["confirmedAt"] == at.isoformat().replace("+00:00", "Z")


# --- suggestions ---------------------------------------------------------------------------------


def signed_in(user: User) -> Api:
    client = Client()
    client.force_login(user)
    return Api(client)


def match_page(user: User, match: JobMatch) -> dict[str, Any]:
    response = signed_in(user).get(f"/api/pages/matches/{match.id}", headers=SSR)
    assert response.status_code == 200, response.content
    return response.json()


def decide(user: User, email_id: Any, action: str) -> Any:
    return signed_in(user).post(f"/api/mail/suggestions/{email_id}", {"action": action})


def test_a_rejection_and_an_invitation_are_suggested_and_move_nothing_until_accepted(
    google_server: FakeGoogle,
) -> None:
    user = make_user()
    acme, globex, initech = (
        applied(user, "Acme"),
        applied(user, "Globex Systems"),
        applied(user, "Initech Labs"),
    )
    google_server.connect(user)
    at = days_ago(0.5).replace(microsecond=0)
    rejection(google_server, "Acme", at=at)
    invitation(google_server, "Globex Systems", at=at)
    invitation(google_server, "Initech Labs", at=at)

    assert run_check(user).matched == 3

    # Reading the mail moved nothing.
    assert [stage_of(m) for m in (acme, globex, initech)] == ["applied"] * 3
    rows = by_subject(user)
    rejected, invited = rows["Your application to Acme"], rows["Interview with Globex Systems"]
    assert (rejected.kind, rejected.match_id, rejected.decision) == ("rejection", acme.id, None)
    assert (invited.kind, invited.match_id, invited.decision) == ("interview", globex.id, None)

    # Each match page puts its own suggestion to the user.
    assert match_page(user, acme)["mail"]["suggestion"] == {
        "id": str(rejected.id),
        "kind": "rejection",
        "stage": "rejected",
        "sender": "Acme Recruiting",
        "receivedAt": at.isoformat().replace("+00:00", "Z"),
        "subject": "Your application to Acme",
    }
    suggestion = match_page(user, globex)["mail"]["suggestion"]
    assert (suggestion["kind"], suggestion["stage"]) == ("interview", "interviewing")
    # Looking at the pages moved nothing either.
    assert [stage_of(m) for m in (acme, globex, initech)] == ["applied"] * 3

    # Move: the match goes where the email suggests, through the usual stage change.
    moved = decide(user, rejected.id, "accept")
    assert (moved.status_code, moved.json()) == (200, {"ok": True, "message": "Moved to Rejected."})
    assert stage_of(acme) == "rejected"
    accepted = ApplicationEmail.objects.for_user(user.pk).get(id=rejected.id)
    assert accepted.decision == "accepted" and accepted.decided_at is not None
    assert match_page(user, acme)["mail"]["suggestion"] is None

    moved = decide(user, invited.id, "accept")
    assert moved.json() == {"ok": True, "message": "Moved to Interviewing."}
    assert stage_of(globex) == "interviewing"

    # Dismiss: nothing moves, and the suggestion is not made again.
    third = rows["Interview with Initech Labs"]
    dismissed = decide(user, third.id, "dismiss")
    assert (dismissed.status_code, dismissed.json()) == (200, {"ok": True, "message": "Dismissed."})
    assert stage_of(initech) == "applied"
    assert match_page(user, initech)["mail"]["suggestion"] is None
    run_check(user)
    assert match_page(user, initech)["mail"]["suggestion"] is None

    # An answer is given once.
    for email_id, action in ((rejected.id, "dismiss"), (third.id, "accept")):
        again = decide(user, email_id, action)
        assert again.status_code == 409
        assert again.json() == {
            "ok": False,
            "message": "That suggestion no longer applies. Reload the page.",
        }
    assert (stage_of(acme), stage_of(initech)) == ("rejected", "applied")


def test_a_suggestion_the_user_got_to_first_is_not_made(google_server: FakeGoogle) -> None:
    user = make_user()
    interviewing = applied(user, "Acme", stage="interviewing")
    google_server.connect(user)
    invitation(google_server, "Acme")
    run_check(user)
    [row] = emails(user)

    # Already interviewing: an invitation suggests nothing, and cannot be "accepted".
    assert (row.kind, row.match_id) == ("interview", interviewing.id)
    assert match_page(user, interviewing)["mail"]["suggestion"] is None
    assert decide(user, row.id, "accept").status_code == 409
    assert stage_of(interviewing) == "interviewing"

    # A rejection still applies to it.
    rejection(google_server, "Acme")
    run_check(user)
    suggestion = match_page(user, interviewing)["mail"]["suggestion"]
    assert (suggestion["kind"], suggestion["stage"]) == ("rejection", "rejected")
    # The user moves the match themselves: the suggestion goes away without being answered.
    JobMatch.objects.for_user(user.pk).filter(id=interviewing.id).update(stage="rejected")
    assert match_page(user, interviewing)["mail"]["suggestion"] is None
    assert decide(user, suggestion["id"], "accept").status_code == 409


def test_only_a_suggestion_can_be_answered_and_only_by_its_owner(google_server: FakeGoogle) -> None:
    user, other = make_user(), make_user()
    match = applied(user, "Acme")
    google_server.connect(user)
    confirmation(google_server, "Acme")
    rejection(google_server, "Acme")
    run_check(user)
    rows = by_subject(user)
    confirmed, rejected = rows["Thank you for applying to Acme"], rows["Your application to Acme"]

    for email_id in (confirmed.id, "not-an-id", "00000000-0000-7000-8000-000000000000"):
        for action in ("accept", "dismiss"):
            response = decide(user, email_id, action)
            assert response.status_code == 404, (email_id, action)
            assert response.json() == {"ok": False, "message": "That suggestion no longer exists."}
    assert decide(other, rejected.id, "accept").status_code == 404
    assert decide(other, rejected.id, "dismiss").status_code == 404
    unread = decide(user, rejected.id, "maybe")
    assert (unread.status_code, unread.json()["message"]) == (400, "That answer could not be read.")
    assert stage_of(match) == "applied"
    assert ApplicationEmail.objects.for_user(user.pk).get(id=rejected.id).decision is None
    assert (
        Api(Client()).post(f"/api/mail/suggestions/{rejected.id}", {"action": "accept"}).status_code
        == 401
    )


def test_the_match_page_reads_mail_once_and_only_where_an_answer_is_expected(
    google_server: FakeGoogle, django_assert_num_queries: Any
) -> None:
    """A match nobody has applied to costs the page nothing; one in `applied` costs one query,
    which is the last of the page's budget (`tests/pages/test_pages.py`)."""
    user = make_user(with_resume=True)
    waiting = make_match(user, job=make_job(company=make_company(name="Globex Systems")))
    sent = applied(user, "Acme")
    google_server.connect(user)
    rejection(google_server, "Acme")
    run_check(user)
    client = Client()
    client.force_login(user)

    # No tailored resume and nobody known at either company: the match, the base resume, the
    # signals, and (#64) the people the user knows there.
    auth, page_without_mail = 2, 4
    with django_assert_num_queries(auth + page_without_mail):
        response = client.get(f"/api/pages/matches/{waiting.id}", headers=SSR)
    assert response.json()["mail"]["suggestion"] is None
    with django_assert_num_queries(auth + page_without_mail + 1):
        response = client.get(f"/api/pages/matches/{sent.id}", headers=SSR)
    assert response.json()["mail"]["suggestion"]["kind"] == "rejection"


# --- other mail ----------------------------------------------------------------------------------


def test_mail_that_matches_no_application_changes_nothing(google_server: FakeGoogle) -> None:
    user = make_user()
    acme = applied(user, "Acme")
    google_server.connect(user)
    newsletter(google_server)
    invitation(google_server, "Zephyrine Staffing")  # a company the user never applied to
    google_server.deliver(
        sender="Dana <dana@mailbox.example>",
        subject="Re: interview outfit??",
        snippet="wear the blue one. did you hear back about the application to Acme?",
    )
    # An employer the user did apply to, writing about something else.
    google_server.deliver(
        sender="Acme <news@greenhouse-mail.io>",
        subject="Acme is hiring: 3 new roles",
        snippet="New this week at Acme. View all jobs and apply today.",
    )

    outcome = run_check(user)

    assert (outcome.stored, outcome.matched) == (4, 0)
    kinds = sorted(row.kind for row in emails(user))
    assert kinds == ["interview", "other", "other", "other"]
    assert all(row.match_id is None for row in emails(user))
    assert stage_of(acme) == "applied" and application_of(acme).confirmed_at is None
    assert match_page(user, acme)["mail"] == {
        "confirmedAt": None,
        "confirmationSubject": None,
        "suggestion": None,
    }


# --- tokens --------------------------------------------------------------------------------------


def test_a_revoked_token_asks_to_be_reconnected_and_is_never_tried_again(
    google_server: FakeGoogle, configured: None, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    user = make_user()
    acme = applied(user, "Acme")
    connected = google_server.connect(user, access_expires_at=days_ago(1))  # time for a refresh
    kept = make_application_email(user, account=connected, kind="rejection", match=acme)
    refresh = crypto.unseal(connected.refresh_token)
    google_server.revoke_grant()  # removed at Google, or seven days passed in "Testing"

    outcome = run_check(user)

    assert (outcome.checked, outcome.failed, outcome.stored) == (0, 1, 0)
    # One request: the refresh Google refused. Nothing was read.
    [request] = google_server.requests
    assert str(request.url) == google.TOKEN_URL
    assert google_server.forms[0]["grant_type"] == "refresh_token"
    row = account(user)
    assert (row.status, row.last_error) == ("reconnect", google.RECONNECT)
    assert row.access_token == "" and row.access_expires_at is None
    assert row.last_checked_at is None

    # It does not loop: neither this user's check nor the sweep asks Google again.
    google_server.requests.clear()
    assert run_check(user).failed == 0
    assert asyncio.run(check.check_all()).checked == 0
    assert google_server.requests == []

    # Settings says so, in a sentence, and what was read before is still there.
    mail = signed_in(user).get("/api/pages/settings", headers=SSR).json()["mail"]
    assert (mail["account"]["status"], mail["account"]["lastError"]) == (
        "reconnect",
        "Google no longer accepts Foray's access to this mailbox: it was revoked, or it "
        "expired. Connect it again.",
    )
    assert mail["account"]["found"]["rejections"] == 1
    assert ApplicationEmail.objects.for_user(user.pk).filter(id=kept.id).exists()
    assert refresh not in caplog.text and "Token has been expired" not in caplog.text

    # Connecting again makes it checkable again.
    api = signed_in(user)
    url = api.post("/api/mail/google/start").json()["url"]
    assert api.get(google_server.consent(url)).status_code == 302
    assert account(user).status == "connected"
    rejection(google_server, "Acme")
    assert run_check(user).checked == 1


def test_an_access_token_gmail_refuses_is_refreshed_once(
    google_server: FakeGoogle, configured: None
) -> None:
    user = make_user()
    applied(user, "Acme")
    connected = google_server.connect(user)
    old = crypto.unseal(connected.access_token)
    google_server.expire_access_tokens()  # Google forgot it before our clock said so
    confirmation(google_server, "Acme")

    outcome = run_check(user)

    assert (outcome.checked, outcome.stored) == (1, 1)
    assert len(google_server.token_requests) == 1
    row = account(user)
    new = crypto.unseal(row.access_token)
    assert new != old and new in google_server.access_tokens
    assert row.access_token.startswith("v1:") and row.status == "connected"
    # The refresh token was not replaced by an answer that carried none.
    assert crypto.unseal(row.refresh_token) == crypto.unseal(connected.refresh_token)

    # Refused even with a token Google has just issued: the user has to reconnect.
    google_server.answer["gmail"] = lambda request: httpx2.Response(
        401, json={"error": {"code": 401, "status": "UNAUTHENTICATED"}}
    )
    google_server.requests.clear()
    assert run_check(user).failed == 1
    assert len(google_server.token_requests) == 1  # once, not a loop
    assert account(user).status == "reconnect"


def test_a_token_this_install_can_no_longer_open_asks_to_be_reconnected(
    google_server: FakeGoogle, settings: Any
) -> None:
    user = make_user()
    google_server.connect(user)
    settings.SECRET_KEY = "a-different-secret-key-0123456789-0123456789-012345"

    assert run_check(user).checked == 0

    assert google_server.requests == []
    row = account(user)
    assert (row.status, row.last_error) == ("reconnect", store.UNREADABLE_TOKEN)


# --- failures that pass --------------------------------------------------------------------------


def _api_error(status: int, reason: str, state: str) -> httpx2.Response:
    body = {
        "code": status,
        "message": "Google's own words",
        "errors": [{"reason": reason}],
        "status": state,
    }
    return httpx2.Response(status, json={"error": body})


@pytest.mark.parametrize(
    ("answer", "sentence"),
    [
        ((429, "rateLimitExceeded", "RESOURCE_EXHAUSTED"), google.RATE_LIMITED),
        ((403, "userRateLimitExceeded", "PERMISSION_DENIED"), google.RATE_LIMITED),
        ((403, "accessNotConfigured", "PERMISSION_DENIED"), google.API_DISABLED),
        ((403, "forbidden", "PERMISSION_DENIED"), google.NOT_ALLOWED),
        ((503, "backendError", "UNAVAILABLE"), google.UNREACHABLE),
        ((400, "invalidArgument", "INVALID_ARGUMENT"), google.NOT_ACCEPTED),
        (None, google.UNREADABLE),  # a 200 that is not JSON
    ],
)
def test_a_failure_that_may_pass_is_a_sentence_and_the_account_stays_connected(
    answer: tuple[int, str, str] | None,
    sentence: str,
    google_server: FakeGoogle,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    user = make_user()
    applied(user, "Acme")
    connected = google_server.connect(user)
    confirmation(google_server, "Acme")
    google_server.answer["gmail"] = lambda request: (
        _api_error(*answer) if answer else httpx2.Response(200, text="<html>")
    )

    outcome = run_check(user)

    assert (outcome.checked, outcome.failed, outcome.stored) == (0, 1, 0)
    row = account(user)
    assert (row.status, row.last_error, row.last_checked_at) == ("connected", sentence, None)
    assert "Google's own words" not in row.last_error
    assert "Google's own words" not in caplog.text
    assert crypto.unseal(connected.access_token) not in caplog.text

    # The next check, with Gmail answering again, clears it and reads what was waiting.
    del google_server.answer["gmail"]
    outcome = run_check(user)
    assert (outcome.checked, outcome.stored) == (1, 1)
    row = account(user)
    assert row.last_error == "" and row.last_checked_at is not None


def test_a_grant_without_the_scope_asks_to_be_reconnected(google_server: FakeGoogle) -> None:
    user = make_user()
    google_server.connect(user)
    google_server.answer["gmail"] = lambda request: _api_error(
        403, "insufficientPermissions", "PERMISSION_DENIED"
    )
    assert run_check(user).failed == 1
    row = account(user)
    assert (row.status, row.last_error) == ("reconnect", google.RECONNECT_SCOPE)


def test_a_check_keeps_what_it_read_before_it_was_stopped(google_server: FakeGoogle) -> None:
    user = make_user()
    applied(user, "Acme")
    google_server.connect(user)
    for _ in range(4):
        confirmation(google_server, "Acme")
    served = {"reads": 0}

    def after_two(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.startswith("/gmail/v1/users/me/messages/"):
            served["reads"] += 1
            if served["reads"] > 2:
                return _api_error(429, "rateLimitExceeded", "RESOURCE_EXHAUSTED")
        return google_server._gmail(request)

    google_server.answer["gmail"] = after_two

    outcome = run_check(user)

    assert (outcome.failed, outcome.stored) == (1, 2)
    assert len(emails(user)) == 2 and account(user).last_error == google.RATE_LIMITED
    # Resumed: the next check reads the two it did not get to, and only those.
    del google_server.answer["gmail"]
    google_server.requests.clear()
    assert run_check(user).stored == 2
    assert len(google_server.reads()) == 2 and len(emails(user)) == 4


def test_a_message_deleted_since_it_was_listed_is_skipped(google_server: FakeGoogle) -> None:
    user = make_user()
    applied(user, "Acme")
    google_server.connect(user)
    gone, kept = confirmation(google_server, "Acme"), rejection(google_server, "Acme")

    def without_one(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith(f"/messages/{gone}"):
            return _api_error(404, "notFound", "NOT_FOUND")
        return google_server._gmail(request)

    google_server.answer["gmail"] = without_one
    outcome = run_check(user)
    assert (outcome.checked, outcome.stored) == (1, 1)
    assert [row.message_id for row in emails(user)] == [kept]


# --- bounded -------------------------------------------------------------------------------------


def test_a_check_is_bounded_and_the_next_one_carries_on(
    google_server: FakeGoogle, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store, "LIMIT", 3)
    google_server.page_size = 2  # so the listing takes several pages
    user = make_user()
    applied(user, "Acme")
    google_server.connect(user)
    delivered = [
        confirmation(google_server, "Acme", at=days_ago(1) + timedelta(minutes=n)) for n in range(7)
    ]
    newest_first = delivered[::-1]

    assert run_check(user).stored == 3
    assert google_server.reads() == newest_first[:3]
    assert run_check(user).stored == 3
    assert google_server.reads() == newest_first[:6]
    assert run_check(user).stored == 1
    assert google_server.reads() == newest_first
    google_server.requests.clear()
    assert run_check(user).stored == 0 and google_server.reads() == []
    assert len(emails(user)) == 7
    # However long the mailbox, a check stops listing after `MAX_PAGES` pages.
    monkeypatch.setattr(check, "MAX_PAGES", 2)
    google_server.requests.clear()
    run_check(user)
    assert len(google_server.gmail_requests) == 2


def test_mail_about_no_application_is_forgotten_after_a_month(google_server: FakeGoogle) -> None:
    """It can no longer be listed (the search looks two weeks back) or matched, so nothing is
    served by keeping it. Mail matched to one of the user's applications stays."""
    user = make_user()
    connected = google_server.connect(user)
    match = applied(user, "Acme", sent=80)

    def kept(kind: str, days: float, **fields: Any) -> ApplicationEmail:
        return make_application_email(
            user, account=connected, kind=kind, received_at=days_ago(days), **fields
        )

    old_other, recent_other = kept("other", 31), kept("other", 5)
    old_unmatched, recent_unmatched = kept("interview", 31), kept("interview", 20)
    old_matched = kept("rejection", 60, match=match)

    assert run_check(user).checked == 1

    left = {row.id for row in emails(user)}
    assert left == {recent_other.id, recent_unmatched.id, old_matched.id}
    assert not left & {old_other.id, old_unmatched.id}


# --- the task ------------------------------------------------------------------------------------


def test_the_task_checks_one_user_or_every_connected_mailbox(google_server: FakeGoogle) -> None:
    first, second, third = make_user(), make_user(), make_user()
    for user in (first, second, third):
        applied(user, "Acme")
    google_server.connect(first)
    google_server.connect(second)
    google_server.connect(third, status="reconnect")
    confirmation(google_server, "Acme")

    # After connecting: that user's mailbox alone.
    connect.defer_check(first.pk)
    connect.defer_check(first.pk)  # one waiting copy is enough
    run_deferred(["system"])
    assert len(emails(first)) == 1 and emails(second) == []
    [job] = jobs(task_name="mail.check")
    assert (job["status"], job["args"]) == ("succeeded", {"user_id": first.pk})

    # The schedule's tick: every connected mailbox, and none that waits to be reconnected.
    defer_in_tx(mail_check, timestamp=1_794_000_420)
    run_deferred(["system"])
    assert len(emails(first)) == 1 and len(emails(second)) == 1 and emails(third) == []
    assert account(third).last_checked_at is None
    # Each user's rows are their own.
    assert {row.user_id for row in emails(second)} == {second.pk}
    assert emails(second)[0].match_id != emails(first)[0].match_id


def test_the_check_runs_every_fifteen_minutes_on_the_system_lane() -> None:
    [entry] = [entry for entry in periodic.SCHEDULE if entry.task == "mail.check"]
    assert (entry.cron, entry.queueing_lock) == ("7-59/15 * * * *", "mail:sweep")
    assert (mail_check.queue, mail_check.lock) == ("system", None)
    # As procrastinate reads that cron: four ticks an hour, off the boards' quarter hours.
    registry = PeriodicRegistry()
    tick: Any = mail_check  # a periodic task takes the tick as `timestamp`
    registry.register_task(task=tick, cron=entry.cron, periodic_id="", configure_kwargs={})
    deferrer = PeriodicDeferrer(registry=registry, max_delay=6 * 3600)
    noon = int(datetime(2026, 11, 9, 12, 0, 30, tzinfo=UTC).timestamp())
    ticks = {
        datetime.fromtimestamp(at, tz=UTC).minute
        for minute in range(60)
        for _, at in deferrer.get_previous_tasks(at=noon + 60 * minute)
    }
    assert ticks == {7, 22, 37, 52}
    # No model is called, so a check a shutdown interrupted may run again; nothing else retries.
    strategy = mail_check.retry_strategy
    assert isinstance(strategy, RetryStrategy) and strategy.max_attempts == 1
    assert set(strategy.retry_exceptions or ()) == {asyncio.CancelledError}
    assert TASKS["mail.check"].timeout_s > check.ACCOUNT_TIMEOUT_S


def test_one_users_trouble_does_not_stop_the_others(
    google_server: FakeGoogle, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    broken, fine = make_user(), make_user()
    applied(fine, "Acme")
    google_server.connect(broken, last_checked_at=days_ago(1))
    google_server.connect(fine, last_checked_at=days_ago(0.5))
    confirmation(google_server, "Acme")
    real = store.record

    def record(user_id: str, *args: Any) -> Any:
        if user_id == broken.pk:
            raise RuntimeError("a bug, with nothing secret in it")
        return real(user_id, *args)

    monkeypatch.setattr(store, "record", record)

    outcome = asyncio.run(check.check_all())

    assert (outcome.checked, outcome.failed) == (1, 1)
    assert account(broken).last_error == check.UNEXPECTED
    assert len(emails(fine)) == 1 and account(fine).last_error == ""
    for secret in secrets_of(google_server):
        assert secret not in caplog.text


def test_nothing_is_checked_without_an_account(google_server: FakeGoogle) -> None:
    user = make_user()
    outcome = run_check(user)
    assert (outcome.checked, outcome.failed, outcome.stored) == (0, 0, 0)
    assert asyncio.run(check.check_all()).checked == 0
    assert google_server.requests == []
    assert settings.ENV.mail_configured is False  # a check needs no client id to read
