"""Which application an email is about (#26): `apps.mail.matching`, a pure function.

The cases are the ones that go wrong in a real mailbox: relays that carry every employer's
mail, employers whose names are ordinary words, an agency writing about a client, a job board
naming five employers, two applications to one employer, and an answer that arrives before the
user has told Foray the application was sent.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from apps.mail.matching import (
    ATS_RELAYS,
    EARLY,
    NOT_AN_EMPLOYER,
    WINDOW,
    Mail,
    Sent,
    find,
    is_employers_word,
    match,
)

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
MESSAGES: list[dict[str, Any]] = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "mail" / "messages.json").read_text()
)


def sent(
    company: str,
    title: str = "Software Engineer",
    *,
    days_ago: float = 3,
    domain: str | None = None,
) -> Sent:
    return Sent(
        match_id=uuid.uuid4(),
        company=company,
        title=title,
        sent_at=NOW - timedelta(days=days_ago),
        domain=domain,
    )


def mail(
    name: str = "",
    domain: str = "greenhouse-mail.io",
    subject: str = "An update",
    *,
    days_ago: float = 1,
) -> Mail:
    return Mail(
        sender_name=name,
        sender_domain=domain,
        subject=subject,
        received_at=NOW - timedelta(days=days_ago),
    )


def evidence(message: Mail, applications: list[Sent]) -> str | None:
    found = find(message, applications)
    return found.evidence if found else None


# --- what counts as evidence ---------------------------------------------------------------------


def test_the_employers_name_on_the_from_line() -> None:
    acme, other = sent("Northwind Robotics"), sent("Halcyon Labs")
    message = mail("Northwind Robotics Hiring Team", subject="Next steps")
    assert match(message, [other, acme]) is acme
    assert evidence(message, [other, acme]) == "sender"
    # A person at the employer, written the way ATS mail writes it.
    assert match(mail("Jordan at Northwind Robotics"), [acme, other]) is acme
    assert match(mail("Somebody Else"), [acme, other]) is None


def test_the_employers_name_in_the_subject_line() -> None:
    acme = sent("Orchard Street Analytics")
    message = mail("Workday", "myworkday.com", "Application Submitted - Orchard Street Analytics")
    assert match(message, [acme, sent("Halcyon Labs")]) is acme
    assert evidence(message, [acme]) == "subject"


def test_the_employers_own_domain() -> None:
    acme = sent("Marrow & Pine", domain="marrowandpine.example")
    for domain in ("marrowandpine.example", "mail.marrowandpine.example", "MarrowAndPine.Example."):
        message = mail("Alex", domain, "Quick question")
        assert match(message, [acme]) is acme
        assert evidence(message, [acme]) == "domain"
    # A domain that only ends with the same letters is someone else's.
    assert match(mail("Alex", "notmarrowandpine.example"), [acme]) is None
    assert match(mail("Alex", "marrowandpine.example.evil.example"), [acme]) is None
    # No known domain, no domain evidence.
    assert match(mail("Alex", "marrowandpine.example"), [sent("Marrow & Pine")]) is None


def test_names_are_compared_as_the_registry_normalises_them() -> None:
    acme = sent("Marrow & Pine Co.")
    assert match(mail("Marrow and Pine Careers"), [acme]) is acme
    assert match(mail("MARROW & PINE"), [acme]) is acme
    inc = sent("Northwind Robotics, Inc.")
    assert match(mail("Northwind Robotics"), [inc]) is inc
    # Whole words only: "Pine" alone is not the company, nor is a longer word.
    assert match(mail("Pine Street Hiring"), [acme]) is None
    assert match(mail("Northwinds Roboticsworks"), [inc]) is None


def test_the_strongest_evidence_decides() -> None:
    """A sender's name beats a subject line: an agency writing about a client is the agency's."""
    agency, client = sent("Zephyrine Staffing"), sent("Fennec Systems")
    message = mail("Zephyrine Staffing", "zephyrine.example", "Update on the Fennec Systems role")
    assert match(message, [client, agency]) is agency
    # And the employer's own domain beats a name on the From line.
    own = sent("Lumenfold", domain="lumenfold.example")
    message = mail("Fennec Systems fans", "lumenfold.example", "hello")
    assert match(message, [client, own]) is own


def test_two_employers_named_equally_is_no_match() -> None:
    # Sent on different days, so "the nearest application" would happily pick one of them.
    a, b = sent("Cobalt Harbor", days_ago=3), sent("Halcyon Labs", days_ago=6)
    message = mail("CareerNest", "careernest.example", "Cobalt Harbor and Halcyon Labs are hiring")
    assert match(message, [a, b]) is None
    assert match(mail("Cobalt Harbor x Halcyon Labs"), [a, b]) is None
    # With only one of them applied to, there is nothing to confuse it with.
    assert match(message, [a]) is a


# --- relays and other people's domains -----------------------------------------------------------


def test_a_relay_carries_no_employer_even_for_someone_who_applied_to_the_relay() -> None:
    """Every Lever customer's mail comes from hire.lever.co. Having applied to Lever itself
    must not make all of it Lever's."""
    lever = sent("Lever", domain="lever.co")
    lumenfold = sent("Lumenfold")
    message = mail("Lumenfold", "hire.lever.co", "Thank you for your application to Lumenfold")
    assert match(message, [lever, lumenfold]) is lumenfold
    assert match(message, [lever]) is None
    # Lever's own recruiters still match, by name.
    assert match(mail("Lever Recruiting", "hire.lever.co"), [lever, lumenfold]) is lever
    for domain in ("greenhouse-mail.io", "us.greenhouse-mail.io", "ashbyhq.com", "myworkday.com"):
        assert match(mail("Someone", domain), [sent("Greenhouse", domain=domain)]) is None


def test_a_public_mailbox_is_nobodys_domain() -> None:
    odd = sent("Tiny Startup", domain="gmail.com")  # a registry row with a careless domain
    assert match(mail("A founder", "gmail.com", "hi"), [odd]) is None
    assert match(mail("Tiny Startup founder", "gmail.com", "hi"), [odd]) is odd
    assert ATS_RELAYS < NOT_AN_EMPLOYER and "gmail.com" in NOT_AN_EMPLOYER - ATS_RELAYS


# --- employers whose names are words -------------------------------------------------------------


@pytest.mark.parametrize(
    ("company", "subject", "matches"),
    [
        ("Remote", "Thank you for applying to Remote", True),
        ("Remote", "Your application at Remote", True),
        ("Remote", "Remote | Application received", True),
        ("Remote", "Application received - Remote", True),
        ("Remote", "Remote's hiring team would like to meet you", True),
        ("Remote", "Remote Backend Engineer application received", False),
        ("Remote", "Your application to the Remote Backend Engineer role", False),
        ("Remote", "Thank you for applying to Remote Backend Engineer", False),
        ("Remote", "Is remote work right for you?", False),
        ("Scale", "Scale your career with these jobs", False),
        ("Scale", "Interview with Scale", True),
        ("Linear", "Re: Linear - phone screen", True),
        ("Linear", "A linear path to promotion", False),
        ("Ramp", "Thanks for applying to Ramp!", True),
        ("Ramp", "Ramp up your interview prep", False),
        # Two words are distinctive enough anywhere in the subject.
        ("Scale Works", "Scale Works application received", True),
        ("Scale Works", "How scale works at startups", True),
        # Too short to trust in a sentence at all.
        ("X", "Thanks for applying to X", False),
    ],
)
def test_a_one_word_name_counts_only_where_a_company_would_stand(
    company: str, subject: str, matches: bool
) -> None:
    application = sent(company)
    message = mail("Recruiting Team", subject=subject)
    assert (match(message, [application]) is application) is matches


def test_a_one_word_name_on_the_from_line_is_enough() -> None:
    remote, x = sent("Remote"), sent("X")
    assert match(mail("Remote"), [remote]) is remote
    assert match(mail("Remote Talent"), [remote]) is remote
    assert match(mail("Remotely Yours"), [remote]) is None
    assert match(mail("X"), [x]) is x  # the whole display name, for a name this short
    assert match(mail("X marks the spot"), [x]) is None


# --- when ----------------------------------------------------------------------------------------


def test_only_within_the_window_after_the_application_was_sent() -> None:
    acme = sent("Halcyon Labs", days_ago=10)
    name = "Halcyon Labs Recruiting"
    assert match(mail(name, days_ago=9), [acme]) is acme
    # The confirmation usually arrives before the user tells Foray "sent": a day's grace.
    assert match(mail(name, days_ago=10.5), [acme]) is acme
    assert match(mail(name, days_ago=11.5), [acme]) is None
    old = sent("Halcyon Labs", days_ago=100)
    assert match(mail(name, days_ago=11), [old]) is old  # day 89
    assert match(mail(name, days_ago=9), [old]) is None  # day 91
    assert (timedelta(days=1), timedelta(days=90)) == (EARLY, WINDOW)


def test_several_applications_to_one_employer() -> None:
    backend = sent("Cobalt Harbor", "Platform Engineer", days_ago=20)
    data = sent("Cobalt Harbor", "Data Analyst II", days_ago=6)
    name = "Cobalt Harbor Careers"
    # The title in the subject line decides.
    message = mail(name, subject="Update regarding your application for Platform Engineer")
    assert match(message, [data, backend]) is backend
    # Without one, the application sent most recently before the message.
    assert match(mail(name, subject="An update", days_ago=1), [backend, data]) is data
    assert match(mail(name, subject="An update", days_ago=10), [backend, data]) is backend
    # One sent after the message (within the grace) loses to one sent before it.
    later = sent("Cobalt Harbor", "Site Reliability Engineer", days_ago=0.5)
    assert match(mail(name, subject="An update", days_ago=1), [later, data]) is data
    # Two sent at the same moment and nothing to tell them apart: no guess.
    twin = Sent(uuid.uuid4(), "Cobalt Harbor", "Data Analyst III", data.sent_at)
    assert match(mail(name, subject="An update"), [data, twin]) is None
    assert match(mail(name, subject="Data Analyst III at Cobalt Harbor"), [data, twin]) is twin


def test_no_applications_no_match() -> None:
    assert match(mail("Halcyon Labs Recruiting"), []) is None
    assert match(mail("", "", ""), [sent("Halcyon Labs")]) is None
    assert match(mail("Halcyon Labs"), [sent("")]) is None


# --- whose word it is ----------------------------------------------------------------------------


def test_the_employers_own_word() -> None:
    acme = sent("Brightloom", domain="brightloom.example")
    by_domain = mail("Alex", "brightloom.example")
    by_sender = mail("Brightloom", "greenhouse-mail.io")
    by_relay_subject = mail("Workday", "myworkday.com", "Application Submitted - Brightloom")
    by_job_board = mail(
        "CareerNest", "careernest.example", "Your application was sent to Brightloom"
    )
    for message, expected in (
        (by_domain, ("domain", True)),
        (by_sender, ("sender", True)),
        (by_relay_subject, ("subject", True)),
        (by_job_board, ("subject", False)),  # a job board's word that it passed it on
    ):
        found = find(message, [acme])
        assert found is not None and found.sent is acme
        assert (found.evidence, is_employers_word(message, found.evidence)) == expected


# --- the fixture set -----------------------------------------------------------------------------

# One application to each employer in the fixtures, sent three days before every message.
EMPLOYERS = [
    "Northwind Robotics",
    "Halcyon Labs",
    "Brightloom",
    "Tessellate",
    "Cobalt Harbor",
    "Fennec Systems",
    "Lumenfold",
    "Kestrel Works",
    "Driftwood AI",
    "Pallas Health",
    "Juniper Grid",
    "Orchard Street Analytics",
    "Marrow & Pine",
    "Quillfeather",
]
DOMAINS = {
    "Northwind Robotics": "northwindrobotics.example",
    "Halcyon Labs": "halcyonlabs.example",
    "Brightloom": "brightloom.example",
    "Tessellate": "tessellate.example",
    "Lumenfold": "lumenfold.example",
    "Kestrel Works": "kestrelworks.example",
    "Driftwood AI": "driftwood-ai.example",
    "Marrow & Pine": "marrowandpine.example",
    "Quillfeather": "quillfeather.example",
    "Orchard Street Analytics": "orchardstreet.example",
}
APPLICATIONS = [sent(name, domain=DOMAINS.get(name)) for name in EMPLOYERS]

# Mail that names an employer the user applied to and is still about no application of theirs
# (or is someone else's word about it): what each is matched to, and whether on the employer's
# own word. Everything from a job board, an agency or a friend is here.
THIRD_PARTIES: dict[str, tuple[str | None, bool]] = {
    "trap-alert-1": (None, False),  # names two employers, in the snippet only
    "trap-newsletter-1": (None, False),
    "trap-marketing-1": (None, False),
    "trap-viewed-1": (None, False),  # the employer is named in the snippet only
    "trap-cold-outreach-1": (None, False),
    "trap-forwarded-1": ("Brightloom", False),  # a job board saying it passed it on
    "trap-personal-1": (None, False),
    "trap-agency-1": ("Fennec Systems", False),  # an agency's word, in a subject line
    "trap-scheduler-1": ("Pallas Health", True),  # sent in the employer's name
    "trap-intro-1": (None, False),  # "(Quillfeather)" in a friend's subject line
    "trap-assessment-1": ("Pallas Health", False),  # a testing service naming the employer
    # A digest whose subject line names the employer. It is classified `other`, so it never
    # gets this far; the matcher alone would take it for the employer's.
    "trap-alert-3": ("Cobalt Harbor", False),
    "trap-marketing-2": (None, False),
    "trap-closed-1": (None, False),
    "trap-cold-outreach-2": (None, False),  # names an employer only in passing, in the snippet
    "trap-webinar-1": (None, False),
    "trap-agency-2": ("Fennec Systems", False),
    "trap-survey-2": (None, False),
}


def _mail_of(message: dict[str, Any]) -> Mail:
    return mail(message["sender_name"], message["sender_domain"], message["subject"])


def test_third_parties_in_the_fixture_set() -> None:
    by_id = {message["id"]: message for message in MESSAGES}
    for message_id, (company, own_word) in THIRD_PARTIES.items():
        found = find(_mail_of(by_id[message_id]), APPLICATIONS)
        assert (found.sent.company if found else None) == company, message_id
        if found is not None:
            assert is_employers_word(_mail_of(by_id[message_id]), found.evidence) is own_word


def test_every_employers_mail_in_the_fixture_set_finds_its_application() -> None:
    """Each message modelled on an ATS or a recruiter is from one of the fourteen employers,
    and is matched to that employer's application on the employer's own word. The one
    exception names no employer anywhere Foray looks, and is matched to nothing."""
    unmatched = []
    for message in MESSAGES:
        if message["style"] == "trap":
            continue
        letter = _mail_of(message)
        found = find(letter, APPLICATIONS)
        if found is None:
            unmatched.append(message["id"])
            continue
        named = found.sent.company
        text = f"{message['sender_name']} {message['subject']} {message['sender_domain']}"
        assert named.replace("&", "").split()[0].lower() in text.lower(), (message["id"], named)
        assert is_employers_word(letter, found.evidence), message["id"]
    # From "Workday", subject "Your application status has changed".
    assert unmatched == ["workday-other-1"]
