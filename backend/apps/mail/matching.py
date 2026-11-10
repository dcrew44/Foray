"""Which application an employer's email is about (#26). Pure: no database, no clock.

`match(mail, sent)` picks one of the applications the user sent, or none. It works from what is
kept of a message (the sender's display name and domain, the subject line, when it arrived)
and from what Foray knows of each application (the employer's name and, when the registry has
it, the employer's own domain; the title; when it was sent).

**Evidence**, strongest first:

1. `domain`: the sender's domain is the employer's own (`stripe.com`, or a subdomain of it).
2. `sender`: the employer's name is in the sender's display name ("Stripe Recruiting").
3. `subject`: the employer's name is in the subject line ("Your application to Stripe").

Names are compared as `jobs.sources.dedupe.normalize_company` writes them, as whole words. The
snippet is never used: it names other employers too easily ("a company similar to Stripe").

Only the strongest evidence found counts, and it has to point at **one** employer. A sender
name beats a subject line, so an agency writing about a client is the agency's; when two
employers are named equally well there is no match, never a guess.

**Relays carry no employer.** Applicant-tracking systems send in the employer's name from
their own domains (`greenhouse-mail.io`, `hire.lever.co`, `ashbyhq.com`, `myworkday.com`, …),
job boards and scheduling tools send from theirs, and people write from public mailboxes
(`gmail.com`). None of those domains says who the employer is, even for someone who applied to
work at Lever: `NOT_AN_EMPLOYER` lists them, and mail from one is matched by name only.

**A short, ordinary name needs its company around it.** "Remote", "Scale" and "Linear" are
employers and also words. In a subject line a one-word name counts only where a company's name
would stand: after "to", "at", "with", "from" or "joining", before "team", "careers" and the
like, or set off at an end ("Linear | Application received").

**The window.** An application can be answered from a day before Foray was told it was sent
(the confirmation usually arrives before the user comes back to say so) until ninety days
after. Among one employer's applications in the window, the one whose title the subject line
names wins; then the one sent most recently before the message; an exact tie is no match.

**Whose word it is.** `is_employers_word()` says whether a match rests on the employer's own
voice: its domain, its name on the From line, or an applicant-tracking system's relay with the
name in the subject. A job board or an agency naming the employer in a subject line is someone
else's word: good enough to suggest a stage, not to record that the application arrived.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from apps.jobs.sources.dedupe import normalize_company, normalize_title

__all__ = [
    "ATS_RELAYS",
    "EARLY",
    "NOT_AN_EMPLOYER",
    "WINDOW",
    "Evidence",
    "Found",
    "Mail",
    "Sent",
    "find",
    "is_employers_word",
    "match",
]

Evidence = Literal["domain", "sender", "subject"]

# How long before "sent" a reply may be dated, and how long after.
EARLY = timedelta(days=1)
WINDOW = timedelta(days=90)

# Applicant-tracking systems that send mail in an employer's name from a domain of their own.
ATS_RELAYS = frozenset(
    {
        "greenhouse-mail.io",
        "greenhouse.io",
        "hire.lever.co",
        "lever.co",
        "ashbyhq.com",
        "myworkday.com",
        "myworkdayjobs.com",
        "workday.com",
        "icims.com",
        "smartrecruiters.com",
        "smartrecruiters-mail.com",
        "jobvite.com",
        "workable.com",
        "workablemail.com",
        "recruitee.com",
        "bamboohr.com",
        "breezy.hr",
        "teamtailor.com",
        "teamtailor-mail.com",
        "gem.com",
        "successfactors.com",
        "successfactors.eu",
        "taleo.net",
        "oraclecloud.com",
        "applytojob.com",
        "rippling.com",
        "pinpointhq.com",
        "comeet.co",
        "jazzhr.com",
    }
)
# Senders whose domain is never an employer's: the relays, job boards, scheduling and testing
# tools, and public mailboxes.
NOT_AN_EMPLOYER = ATS_RELAYS | frozenset(
    {
        "linkedin.com",
        "indeed.com",
        "indeedemail.com",
        "glassdoor.com",
        "ziprecruiter.com",
        "wellfound.com",
        "angel.co",
        "dice.com",
        "monster.com",
        "otta.com",
        "welcometothejungle.com",
        "calendly.com",
        "goodtime.io",
        "hackerrank.com",
        "codesignal.com",
        "codility.com",
        "coderpad.io",
        "karat.com",
        "gmail.com",
        "googlemail.com",
        "google.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "yahoo.com",
        "icloud.com",
        "me.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
        "fastmail.com",
        "hey.com",
    }
)

_RANK: dict[Evidence, int] = {"domain": 3, "sender": 2, "subject": 1}
_NOT_WORD = re.compile(r"[^a-z0-9]+")
# Shorter than this and a name is matched only as the whole display name.
_MIN_NAME = 3


@dataclass(frozen=True, slots=True)
class Sent:
    """One application the user sent, as the matcher needs it."""

    match_id: uuid.UUID
    company: str  # the posting's employer, as written
    title: str
    sent_at: datetime
    domain: str | None = None  # the employer's own domain, when the registry knows it


@dataclass(frozen=True, slots=True)
class Mail:
    """What is kept of a message."""

    sender_name: str
    sender_domain: str
    subject: str
    received_at: datetime


@dataclass(frozen=True, slots=True)
class Found:
    sent: Sent
    evidence: Evidence


def _words(text: str) -> str:
    """Lowercase words separated by single spaces, padded so `" name " in` means whole words."""
    return f" {_NOT_WORD.sub(' ', text.lower().replace('&', ' and ')).strip()} "


def _under(domain: str, parent: str) -> bool:
    return domain == parent or domain.endswith(f".{parent}")


def _is(domain: str, group: frozenset[str]) -> bool:
    return any(_under(domain, parent) for parent in group)


def _from_own_domain(mail: Mail, sent: Sent) -> bool:
    sender = mail.sender_domain.strip().lower().rstrip(".")
    own = (sent.domain or "").strip().lower().rstrip(".")
    if not sender or not own or "." not in own:
        return False
    if _is(sender, NOT_AN_EMPLOYER) or _is(own, NOT_AN_EMPLOYER):
        return False
    return _under(sender, own)


def _named_by_sender(name: str, mail: Mail) -> bool:
    shown = _words(mail.sender_name)
    if len(name) < _MIN_NAME:
        return shown.strip() == name
    return f" {name} " in shown


def _named_in_subject(name: str, subject: str) -> bool:
    if len(name) < _MIN_NAME:
        return False
    if " " in name:
        return f" {name} " in _words(subject)
    # One word: only where a company's name would stand.
    token = re.escape(name)
    plain = subject.lower()
    return bool(
        re.search(
            rf"(?:\b(?:to|at|with|from|joining|@)\s+{token}\b(?!\s+(?:\w+\s+)?"
            rf"(?:engineer|developer|manager|designer|analyst|scientist|role|position|job)\b)"
            rf"|\b{token}(?:'s)?\s+(?:team|recruiting|recruitment|careers|hiring|talent|people|hr)\b"
            rf"|^\W*(?:(?:re|fwd?)\s*:\s*)*{token}\s*[:|\-–—!<]"
            rf"|[:|\-–—>]\s*{token}\W*$)",
            plain,
        )
    )


def _evidence(mail: Mail, sent: Sent) -> Evidence | None:
    if _from_own_domain(mail, sent):
        return "domain"
    name = normalize_company(sent.company)
    if not name:
        return None
    if _named_by_sender(name, mail):
        return "sender"
    if _named_in_subject(name, mail.subject):
        return "subject"
    return None


def _in_window(mail: Mail, sent: Sent) -> bool:
    return sent.sent_at - EARLY <= mail.received_at <= sent.sent_at + WINDOW


def _names_title(mail: Mail, sent: Sent) -> bool:
    title = normalize_title(sent.title)
    subject = _NOT_TITLE.sub(" ", mail.subject.lower())
    return bool(title) and f" {title} " in f" {' '.join(subject.split())} "


_NOT_TITLE = re.compile(r"[^a-z0-9+#]+")


def _nearest(mail: Mail, candidates: Sequence[Sent]) -> Sent | None:
    """The application sent most recently before the message, else the first sent after it.
    None when two are equally near."""

    def distance(sent: Sent) -> tuple[int, timedelta]:
        before = sent.sent_at <= mail.received_at
        return (0 if before else 1, abs(mail.received_at - sent.sent_at))

    ranked = sorted(candidates, key=distance)
    if len(ranked) > 1 and distance(ranked[0]) == distance(ranked[1]):
        return None
    return ranked[0]


def find(mail: Mail, sent: Sequence[Sent]) -> Found | None:
    """The application `mail` is about and what says so, or None."""
    scored: list[tuple[Sent, Evidence]] = []
    for candidate in sent:
        if not _in_window(mail, candidate):
            continue
        evidence = _evidence(mail, candidate)
        if evidence is not None:
            scored.append((candidate, evidence))
    if not scored:
        return None
    best = max(scored, key=lambda pair: _RANK[pair[1]])[1]
    strongest = [candidate for candidate, evidence in scored if evidence == best]
    if len({normalize_company(candidate.company) for candidate in strongest}) > 1:
        return None  # two employers fit equally: no guess
    titled = [candidate for candidate in strongest if _names_title(mail, candidate)]
    chosen = _nearest(mail, titled or strongest)
    return Found(chosen, best) if chosen is not None else None


def match(mail: Mail, sent: Sequence[Sent]) -> Sent | None:
    """One of `sent`, or None rather than a guess (see the module docstring)."""
    found = find(mail, sent)
    return found.sent if found is not None else None


def is_employers_word(mail: Mail, evidence: Evidence) -> bool:
    """Whether a match made on `evidence` rests on the employer's own voice."""
    if evidence in ("domain", "sender"):
        return True
    return _is(mail.sender_domain.strip().lower().rstrip("."), ATS_RELAYS)
