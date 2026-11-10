"""The mail classifier against its fixture set (#26), and the rules one at a time.

**The fixtures** (`tests/fixtures/mail/messages.json`) are made-up messages, written for this
repository: no sentence is copied from a real email, and nobody in them is a real person (the
applicant is "Sam"; senders have first names only). The employers (Northwind Robotics,
Halcyon Labs, Brightloom, …) and job boards (Jobhop, HireLadder, CareerNest) are invented
names; if one happens to belong to a real company, nothing here is about it. No address
appears at all: a message carries the sender's display name and domain only. The relay domains
(`greenhouse-mail.io`, `hire.lever.co`, `ashbyhq.com`, `myworkday.com`) and the display name
"Workday" are the applicant-tracking systems' own, because telling a relay from an employer
is part of what is tested; every other domain ends in `.example`. A message is what Foray
would keep of it: the sender's display name and domain, the subject, and the snippet as Gmail
gives it (HTML entities included, cut where Gmail would cut it). `kind` is what a person who
read the whole message would call it, not what the classifier says.

**How the set was made, and what its numbers mean.** The same author wrote the patterns and
the messages, so every number here flatters the classifier compared with real mail.

- Batch 1 (53 messages) was written with the first patterns, which were adjusted to it.
- Batch 2 (25) was written next, in other wording; the patterns were adjusted to it as well.
- Batch 3 (25) was written with the patterns frozen and scored once: **12 of 25**. Then the
  patterns were generalised from its misses, so it is `tuned` now too.
- Batch 4 (25, `"set": "fresh"`) was written with the patterns frozen again and scored once:
  **13 of 25**. Every miss but two was a real answer read as `other`; the two were job-board
  mail read as a confirmation. Those two patterns were then narrowed (the only change made
  after scoring, and one that can only reduce what is called a confirmation), which is the
  15 of 25 pinned below. Nothing was done about the ten it still misses.

So: on wording it was tuned to, almost everything; on wording it had not seen, about half,
and nearly always wrong in the quiet direction (an answer left as `other`, where nothing
happens). `KNOWN_MISSES` pins every disagreement by id, so a pattern change that fixes or adds
one fails here until the list says so. Tuning the patterns on the `fresh` batch stops it
being a test: write a new batch instead.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from apps.mail.classify import KINDS, classify, clean, explain

MESSAGES: list[dict[str, Any]] = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "mail" / "messages.json").read_text()
)
STYLES = {"greenhouse", "lever", "ashby", "workday", "recruiter", "trap"}

# id -> what the classifier says instead of the truth. Three of the tuned set, all rejections
# whose snippet ends before the decision; ten of the fresh batch.
KNOWN_MISSES: dict[str, str] = {
    # The snippet is all thanks: nothing in it says which way this went.
    "ashby-rejection-2": "other",
    "workday-rejection-5": "other",
    # The same, under a subject line that reads as a confirmation.
    "trap-truncated-1": "confirmation",
    # Fresh, scored once with the patterns frozen: wording no pattern covers.
    "workday-rejection-6": "other",  # "will not be inviting you to interview"
    "greenhouse-rejection-8": "other",  # "closing out all open applications, including yours"
    "ashby-rejection-6": "other",  # "we will be ending the process here"
    "greenhouse-rejection-9": "other",  # "we don't have a role that matches your experience"
    "trap-agency-2": "other",  # "decided to go with an internal transfer"
    "greenhouse-rejection-10": "other",  # "we have offered the role to someone else"
    "recruiter-interview-8": "other",  # "I would enjoy a quick conversation. Could you do …"
    "recruiter-interview-9": "other",  # thanks for the last chat, then "would like to meet you"
    "lever-confirmation-5": "other",  # "is safely in our system"
    "recruiter-confirmation-5": "other",  # "I have added it to our system"
}


def said(message: dict[str, Any]) -> str:
    return classify(message["subject"], message["snippet"])


def confusion(messages: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """{truth: {said: count}}, every kind present."""
    counts = Counter((message["kind"], said(message)) for message in messages)
    return {truth: {kind: counts[(truth, kind)] for kind in KINDS} for truth in KINDS}


def batch(which: str) -> list[dict[str, Any]]:
    return [message for message in MESSAGES if message["set"] == which]


# --- the set itself ------------------------------------------------------------------------------


def test_the_fixture_set_is_made_up_and_covers_every_style() -> None:
    assert len(MESSAGES) >= 60
    assert len({message["id"] for message in MESSAGES}) == len(MESSAGES)
    assert {message["style"] for message in MESSAGES} == STYLES
    for style in STYLES - {"trap"}:
        kinds = {message["kind"] for message in MESSAGES if message["style"] == style}
        assert {"confirmation", "rejection", "interview"} <= kinds, style
    assert sum(message["style"] == "trap" for message in MESSAGES) >= 15
    relays = {"greenhouse-mail.io", "hire.lever.co", "ashbyhq.com", "myworkday.com"}
    for message in MESSAGES:
        assert message["kind"] in KINDS and message["set"] in {"tuned", "fresh"}
        domain = message["sender_domain"]
        assert domain in relays or domain.endswith(".example"), message["id"]
        assert "@" not in message["sender_name"] + message["subject"] + message["snippet"]
        assert len(message["snippet"]) <= 300


# --- measured ------------------------------------------------------------------------------------


def test_every_disagreement_with_a_person_is_pinned() -> None:
    wrong = {m["id"]: said(m) for m in MESSAGES if said(m) != m["kind"]}
    assert wrong == KNOWN_MISSES


def test_on_wording_it_was_tuned_to() -> None:
    tuned = batch("tuned")
    assert len(tuned) == 103
    assert confusion(tuned) == {
        "confirmation": {"confirmation": 27, "rejection": 0, "interview": 0, "other": 0},
        "rejection": {"confirmation": 1, "rejection": 26, "interview": 0, "other": 2},
        "interview": {"confirmation": 0, "rejection": 0, "interview": 24, "other": 0},
        "other": {"confirmation": 0, "rejection": 0, "interview": 0, "other": 23},
    }


def test_on_wording_it_had_not_seen() -> None:
    """The fresh batch: three in five right, and every miss a real answer left as `other`."""
    fresh = batch("fresh")
    assert len(fresh) == 25
    assert confusion(fresh) == {
        "confirmation": {"confirmation": 3, "rejection": 0, "interview": 0, "other": 2},
        "rejection": {"confirmation": 0, "rejection": 2, "interview": 0, "other": 6},
        "interview": {"confirmation": 0, "rejection": 0, "interview": 4, "other": 2},
        "other": {"confirmation": 0, "rejection": 0, "interview": 0, "other": 6},
    }


def test_what_it_calls_something_is_that_thing() -> None:
    """Precision, over the whole set: a label other than `other` is wrong once in 87, and that
    once is a rejection read as a confirmation, never the reverse."""
    table = confusion(MESSAGES)
    labelled = {kind: sum(table[truth][kind] for truth in KINDS) for kind in KINDS}
    assert (labelled["rejection"], table["rejection"]["rejection"]) == (28, 28)
    assert (labelled["interview"], table["interview"]["interview"]) == (28, 28)
    assert (labelled["confirmation"], table["confirmation"]["confirmation"]) == (31, 30)
    # Nothing that is not an employer's answer is ever called one.
    assert table["other"] == {"confirmation": 0, "rejection": 0, "interview": 0, "other": 29}


# --- the rules -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("subject", "snippet", "kind", "rule"),
    [
        # 1. Not an answer.
        ("5 new jobs for you", "Apply now to roles picked for you.", "other", "not-an-answer"),
        ("Finish your application", "You started an application.", "other", "not-an-answer"),
        ("Verify your email", "Enter this verification code.", "other", "not-an-answer"),
        # 2. A rejection, even under a confirmation's words and after an interview.
        (
            "Thank you for applying",
            "Thank you for applying. Unfortunately, we will not be moving forward.",
            "rejection",
            "rejection",
        ),
        (
            "Your interview",
            "Thank you for interviewing with us. We have chosen another candidate.",
            "rejection",
            "rejection",
        ),
        ("Update", "We are unable to offer you a position at this time.", "rejection", "rejection"),
        ("Update", "The role has been filled.", "rejection", "rejection"),
        # ... but not small print about a case, or about applicants in general.
        (
            "Application received",
            "If you do not hear from us, we have moved forward with other candidates.",
            "confirmation",
            "receipt",
        ),
        (
            "Application received",
            "Applicants not selected for an interview will not be contacted.",
            "confirmation",
            "receipt",
        ),
        # 3. An offer is none of the three.
        ("Your offer", "We are delighted to extend you an offer.", "other", "offer"),
        # 4. An invitation, not a promise of one, and not the note that follows one.
        (
            "Next steps",
            "We would like to schedule a phone screen with you.",
            "interview",
            "invitation",
        ),
        ("Hello", "Are you available for a call on Tuesday?", "interview", "invitation"),
        ("Interview with Northwind Robotics", "Details inside.", "interview", "subject"),
        (
            "Thanks for applying",
            "We will reach out to schedule an interview if there is a fit.",
            "confirmation",
            "thanks-subject",
        ),
        ("Thank you", "Thank you for taking the time to interview with us.", "other", "nothing"),
        ("Re: interview outfit??", "wear the blue one", "other", "nothing"),
        # 5. How a rejection begins when the snippet stops short.
        ("Your application", "After careful consideration, we", "rejection", "soft-rejection"),
        # 6. A confirmation needs a statement of receipt, or thanks where a template puts them.
        ("Hello", "We have received your application.", "confirmation", "receipt"),
        (
            "Hello",
            "Thanks for applying! Our team will review your resume.",
            "confirmation",
            "under-review",
        ),
        ("Thank you for applying to Acme", "Hi Sam", "confirmation", "thanks-subject"),
        (
            "Hello",
            "Thanks for applying. We will be in touch.",
            "confirmation",
            "thanks-and-follow-up",
        ),
        # Thanks in the snippet alone is also how a rejection cut short reads.
        (
            "Your application",
            "Thank you for applying to Acme. We appreciate the time",
            "other",
            "nothing",
        ),
        # "After reviewing your application" starts a decision that is not visible yet.
        ("Your application", "After reviewing your application, we", "other", "nothing"),
        # Reviewing a resume is not about an application unless something says so.
        ("Live event", "Recruiters will review your resume live.", "other", "nothing"),
        ("", "", "other", "nothing"),
    ],
)
def test_the_rules_in_order(subject: str, snippet: str, kind: str, rule: str) -> None:
    assert explain(subject, snippet) == (kind, rule)


def test_text_is_read_as_a_person_would_see_it() -> None:
    # Gmail escapes its snippet, typographers curl their quotes, marketers pad with nothing.
    assert clean("We&#39;ve  received\n your application &amp; resume") == (
        "We've received your application & resume"
    )
    assert clean("we’ve decided") == "we've decided"
    assert clean("Hi‌ ‌ ‌Sam﻿") == "Hi Sam"
    assert (
        classify("Update", "We won&#39;t be moving forward with your application.") == "rejection"
    )
    assert classify("Update", "We won’t be moving forward with your application.") == "rejection"


def test_it_is_deterministic_and_reads_nothing_else() -> None:
    for message in MESSAGES:
        assert said(message) == said(message)
        # The sender is no part of the question: only the two strings are.
        assert explain(message["subject"], message["snippet"])[0] == said(message)
