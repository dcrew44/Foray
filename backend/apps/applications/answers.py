"""Saved answers (#21): the questions application forms keep asking, answered once.

Three things live here, all deterministic:

- `CATALOGUE`: the shared questions, in the words the Profile page asks them, with how each is
  answered (yes or no, one of a few choices, or free text).
- `classify`: which shared question a form's label is asking. A label that is none of them is
  `custom`, and is kept under its own words (`question_key`), so the same question from another
  employer finds the answer.
- `plan`: the answer to give a field. It comes from what the user saved or it does not come at
  all. Nothing here writes an answer for the user, least of all about sponsorship, pay or
  years of experience, and a voluntary disclosure nobody answered is declined (`DECLINE`).

`work_status` reads the four answers that decide whether someone may be hired at all, for the
search to use (`matching.prefilter`): a posting for citizens only is not worth scoring for
someone who said they need sponsorship.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from django.db import transaction
from django.db.models.functions import Now

from apps.applications.models import AnswerKind, ApplicationAnswer

__all__ = [
    "CATALOGUE",
    "DECLINE",
    "EEO",
    "Planned",
    "Question",
    "SavedAnswers",
    "WorkStatus",
    "choose_option",
    "classify",
    "load",
    "canonical",
    "plan",
    "question_key",
    "remove",
    "save",
    "work_status",
]

type Input = Literal["yes_no", "choice", "text"]

# What a voluntary disclosure is answered with when the user saved nothing.
DECLINE = "Decline to answer"
MAX_ANSWER = 4000
MAX_QUESTION = 1000


@dataclass(frozen=True, slots=True)
class Question:
    kind: AnswerKind
    label: str  # the question, as the Profile page asks it
    input: Input
    options: tuple[tuple[str, str], ...] = ()  # (stored value, label) for a choice
    group: Literal["work", "logistics", "links", "disclosures"] = "logistics"
    hint: str = ""


YES_NO = (("yes", "Yes"), ("no", "No"))
K = AnswerKind

CATALOGUE: tuple[Question, ...] = (
    Question(
        K.WORK_AUTHORIZATION,
        "Are you legally authorized to work in the United States?",
        "yes_no",
        group="work",
    ),
    Question(
        K.SPONSORSHIP,
        "Will you now or in the future require sponsorship for a work visa?",
        "yes_no",
        group="work",
        hint="Postings that say they will not sponsor are left out of your searches when yes.",
    ),
    Question(
        K.CITIZENSHIP,
        "Are you a US citizen or permanent resident?",
        "choice",
        (
            ("citizen", "US citizen"),
            ("permanent_resident", "Permanent resident"),
            ("neither", "Neither"),
        ),
        group="work",
        hint="Some roles are open to citizens only, or to citizens and permanent residents.",
    ),
    Question(
        K.CLEARANCE,
        "Do you hold an active US security clearance?",
        "choice",
        (
            ("none", "No"),
            ("secret", "Secret"),
            ("top_secret", "Top Secret"),
            ("ts_sci", "TS/SCI"),
        ),
        group="work",
    ),
    Question(
        K.SALARY,
        "What salary do you expect?",
        "text",
        hint="Written into forms exactly as you put it here.",
    ),
    Question(K.START_DATE, "When can you start?", "text"),
    Question(K.YEARS_EXPERIENCE, "How many years of relevant experience do you have?", "text"),
    Question(K.RELOCATION, "Are you willing to relocate?", "yes_no"),
    Question(K.LOCATION, "Where do you live (city and state)?", "text"),
    Question(K.PHONE, "What phone number should employers call?", "text"),
    Question(K.OVER_18, "Are you at least 18 years old?", "yes_no"),
    Question(
        K.PREVIOUSLY_EMPLOYED,
        "Have you worked for the company you are applying to before?",
        "yes_no",
        hint="Used for every employer. Change the answer on the form when it differs.",
    ),
    Question(K.HOW_HEARD, "How did you hear about the role?", "text"),
    Question(K.LINKEDIN, "LinkedIn profile", "text", group="links"),
    Question(K.GITHUB, "GitHub profile", "text", group="links"),
    Question(K.WEBSITE, "Website or portfolio", "text", group="links"),
    Question(K.PRONOUNS, "Pronouns", "text", group="disclosures"),
    Question(K.GENDER, "Gender", "text", group="disclosures"),
    Question(K.RACE, "Race or ethnicity", "text", group="disclosures"),
    Question(K.HISPANIC, "Are you Hispanic or Latino?", "yes_no", group="disclosures"),
    Question(K.VETERAN, "Veteran status", "text", group="disclosures"),
    Question(K.DISABILITY, "Disability status", "text", group="disclosures"),
)
BY_KIND = {question.kind: question for question in CATALOGUE}
# Voluntary self-identification: declined unless the user saved an answer.
EEO = frozenset({K.GENDER, K.RACE, K.HISPANIC, K.VETERAN, K.DISABILITY, K.PRONOUNS})


# --- which question a label is asking ------------------------------------------------------------

# First match wins, so the narrower question comes first: "authorized to work without
# sponsorship" is about sponsorship, and "veteran" is not "how did you hear".
_PATTERNS: tuple[tuple[AnswerKind, re.Pattern[str]], ...] = tuple(
    (kind, re.compile(pattern, re.IGNORECASE))
    for kind, pattern in (
        (K.SPONSORSHIP, r"sponsor|visa (?:status|support|transfer)|\bh-?1b\b|work permit"),
        (K.CLEARANCE, r"security clearance|\bclearance\b"),
        (K.CITIZENSHIP, r"\bcitizen|permanent resident|green card|u\.?s\.? person\b"),
        (
            K.WORK_AUTHORIZATION,
            r"authori[sz]ed to work|legally (?:able|eligible|permitted|entitled) to work"
            r"|eligib\w+ to work|right to work|work authori[sz]ation",
        ),
        (K.DISABILITY, r"disabilit"),
        (K.VETERAN, r"veteran|military (?:status|service)|armed forces"),
        (K.HISPANIC, r"hispanic|latin[oax]"),
        (K.RACE, r"\brace\b|ethnic"),
        (K.PRONOUNS, r"pronoun"),
        (K.GENDER, r"\bgender\b|\bsex\b|transgender"),
        (
            K.SALARY,
            r"salary|compensation|pay (?:expectation|requirement|range)|desired pay"
            r"|expected (?:pay|wage|rate)",
        ),
        (K.YEARS_EXPERIENCE, r"(?:how many )?years of .{0,40}experience"),
        (
            K.START_DATE,
            r"start date|when (?:can|could|would) you (?:be able to )?start|available to start"
            r"|earliest (?:start|availability)|notice period",
        ),
        (K.RELOCATION, r"relocat"),
        (
            K.PREVIOUSLY_EMPLOYED,
            r"(?:previously|ever|formerly) (?:been )?(?:employed|worked)|worked (?:for|at) .{0,40}"
            r"(?:before|previously|in the past)|former employee",
        ),
        (K.OVER_18, r"(?:at least|over|older than) (?:18|eighteen)|18 years (?:of age|or older)"),
        (
            K.HOW_HEARD,
            r"how did you (?:hear|find|learn)|where did you (?:hear|find)|referral source",
        ),
        (K.LINKEDIN, r"linked ?in"),
        (K.GITHUB, r"git ?hub"),
        (K.WEBSITE, r"website|portfolio|personal (?:site|url)|other (?:url|link)"),
        (K.PHONE, r"phone|mobile number|cell number"),
        (
            K.LOCATION,
            r"where (?:are|do) you (?:currently )?(?:located|live|based)|current (?:location|city)"
            r"|city (?:and|,|/) ?state|location \(city\)",
        ),
    )
)
_SPACE = re.compile(r"\s+")
_NOT_WORDS = re.compile(r"[^a-z0-9 ]+")


def classify(label: str) -> AnswerKind:
    """The shared question a form's label asks, or `custom`."""
    text = _SPACE.sub(" ", label).strip()
    for kind, pattern in _PATTERNS:
        if pattern.search(text):
            return kind
    return K.CUSTOM


def question_key(label: str) -> str:
    """A custom question's identity: its words, lowercased, without punctuation or an
    employer's spacing, so "Why do you want to work here?" matches itself wherever it is asked."""
    words = _SPACE.sub(" ", _NOT_WORDS.sub(" ", label.lower())).strip()
    return hashlib.sha1(words.encode(), usedforsecurity=False).hexdigest()[:20]


# --- the saved answers ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SavedAnswers:
    """One user's answers: the shared questions by kind, custom ones by their key."""

    shared: dict[str, str]
    custom: dict[str, str]

    def get(self, kind: AnswerKind | str, label: str = "") -> str | None:
        if kind == K.CUSTOM:
            return self.custom.get(question_key(label))
        return self.shared.get(str(kind))


def load(user_id: str) -> SavedAnswers:
    """Sync; one query."""
    shared: dict[str, str] = {}
    custom: dict[str, str] = {}
    rows = ApplicationAnswer.objects.for_user(user_id).values_list("kind", "question_key", "answer")
    for kind, key, answer in rows:
        if kind == K.CUSTOM:
            custom[key] = answer
        else:
            shared[kind] = answer
    return SavedAnswers(shared, custom)


def _clean(kind: AnswerKind, answer: str) -> str:
    """An answer as stored: trimmed, and for a yes/no or a choice one of the question's own
    values. Raises `ValueError` for anything else."""
    text = _SPACE.sub(" ", answer).strip()[:MAX_ANSWER]
    question = BY_KIND.get(kind)
    if question is None or question.input == "text":
        return answer.strip()[:MAX_ANSWER]
    allowed = [value for value, _ in (question.options or YES_NO)]
    if text.lower() not in allowed:
        raise ValueError(f"{kind}: {answer!r} is not one of {allowed}")
    return text.lower()


def save(user_id: str, kind: AnswerKind | str, answer: str, question: str = "") -> bool:
    """Saves one answer; an empty one forgets it. A custom answer needs its question. Returns
    whether an answer is now saved. Raises `ValueError` for an answer the question cannot
    take. Sync."""
    kind = AnswerKind(kind)
    if kind == K.CUSTOM:
        question = _SPACE.sub(" ", question).strip()[:MAX_QUESTION]
        if not question:
            raise ValueError("a custom answer needs its question")
        key = question_key(question)
    else:
        question, key = BY_KIND[kind].label, ""
    rows = ApplicationAnswer.objects.for_user(user_id).filter(kind=kind, question_key=key)
    if not answer.strip():
        rows.delete()
        return False
    cleaned = _clean(kind, answer)
    with transaction.atomic():
        if not rows.update(answer=cleaned, question=question, updated_at=Now()):
            ApplicationAnswer.objects.create_for(
                user_id, kind=kind, question_key=key, question=question, answer=cleaned
            )
    return True


def remove(user_id: str, answer_id: str) -> bool:
    deleted, _ = ApplicationAnswer.objects.for_user(user_id).filter(id=answer_id).delete()
    return bool(deleted)


# --- answering a field ---------------------------------------------------------------------------

type Source = Literal["saved", "declined", "missing"]


@dataclass(frozen=True, slots=True)
class Planned:
    """What to put in one field, and where it came from. `value` is None when nothing may be
    entered: the user has to answer first (`missing`)."""

    kind: AnswerKind
    value: str | None
    source: Source


_YES = re.compile(r"^\s*(?:yes|y|true|i (?:am|do|will|have))\b", re.IGNORECASE)
_NO = re.compile(r"^\s*(?:no|n|false|i (?:am not|do not|don't|will not|won't|have not))\b", re.I)
_DECLINE = re.compile(
    r"decline|prefer not|do(?:n't| not) wish|choose not|not (?:wish|want) to|rather not"
    r"|not to (?:say|answer|disclose|self.identify)",
    re.IGNORECASE,
)
_LABELS = {value: label for question in CATALOGUE for value, label in question.options}


def _same(a: str, b: str) -> bool:
    return _NOT_WORDS.sub(" ", a.lower()).split() == _NOT_WORDS.sub(" ", b.lower()).split()


def choose_option(answer: str, options: Sequence[str]) -> str | None:
    """The one of a field's options that says what a saved answer says, or None when none
    does: the same words, a yes for a yes, the option that declines for a decline, or the only
    option that contains the answer."""
    for option in options:
        if _same(option, answer) or _same(option, _LABELS.get(answer, answer)):
            return option
    for pattern in (_YES, _NO):
        if pattern.match(answer):
            matching = [option for option in options if pattern.match(option)]
            if len(matching) == 1:
                return matching[0]
    if _DECLINE.search(answer):
        declining = [option for option in options if _DECLINE.search(option)]
        if declining:
            return declining[0]
    wanted = _LABELS.get(answer, answer).lower()
    containing = [option for option in options if wanted and wanted in option.lower()]
    return containing[0] if len(containing) == 1 else None


def plan(saved: SavedAnswers, label: str, options: Sequence[str] = ()) -> Planned:
    """The answer for a form field with this label, and with these options when it is a
    choice. A voluntary disclosure nobody answered is declined; anything else nobody answered
    is `missing`, as is a saved answer none of the field's options says."""
    kind = classify(label)
    # An answer given to this very wording wins over the shared one: it is how a person pins
    # the option a particular form wants.
    answer = saved.custom.get(question_key(label)) or saved.get(kind, label)
    source: Source = "saved"
    if answer is None:
        if kind not in EEO:
            return Planned(kind, None, "missing")
        answer, source = DECLINE, "declined"
    if not options:
        return Planned(kind, _LABELS.get(answer, answer) if kind != K.CUSTOM else answer, source)
    chosen = choose_option(answer, options)
    if chosen is None:
        return Planned(kind, None, "missing")
    return Planned(kind, chosen, source)


def canonical(kind: AnswerKind | str, value: str) -> str | None:
    """What an answer given on a form says about the shared question it asked, in the form
    that question is stored in, or None when that cannot be told: "Yes, I am authorized" is a
    yes; one of three wordings about citizenship is not guessed at."""
    question = BY_KIND.get(AnswerKind(kind)) if kind in AnswerKind.values else None
    if question is None:
        return None
    if question.input == "text":
        return value.strip() or None
    if question.input == "yes_no":
        if _YES.match(value):
            return "yes"
        if _NO.match(value):
            return "no"
    return None


# --- who may be hired ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkStatus:
    """The four answers that decide eligibility; None is "not said"."""

    authorized: bool | None = None
    needs_sponsorship: bool | None = None
    citizenship: str | None = None  # citizen, permanent_resident, neither
    clearance: str | None = None  # none, secret, top_secret, ts_sci

    @property
    def excluded_work_auth(self) -> list[str]:
        """The `jobs.work_auth` readings this person cannot meet, by their own answers. Only
        what they said rules anything out."""
        excluded: set[str] = set()
        if self.needs_sponsorship:
            excluded |= {"citizen_only", "us_person", "no_sponsorship"}
        if self.citizenship == "permanent_resident":
            excluded.add("citizen_only")
        elif self.citizenship == "neither":
            excluded |= {"citizen_only", "us_person"}
        return sorted(excluded)

    @property
    def lacks_clearance(self) -> bool:
        """Whether they said, in so many words, that they hold no clearance."""
        return self.clearance == "none"


def _yes(value: str | None) -> bool | None:
    return None if value is None else value == "yes"


def work_status(saved: SavedAnswers) -> WorkStatus:
    return WorkStatus(
        authorized=_yes(saved.get(K.WORK_AUTHORIZATION)),
        needs_sponsorship=_yes(saved.get(K.SPONSORSHIP)),
        citizenship=saved.get(K.CITIZENSHIP),
        clearance=saved.get(K.CLEARANCE),
    )
