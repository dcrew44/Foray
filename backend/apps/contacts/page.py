"""The Contacts page, and the board lookup it offers (#63). Sync.

`contacts_page(user_id, query)` answers `GET /api/pages/contacts` in at most four reads: the
user's contacts, the registry (`link.load_index`), the open postings of the companies shown,
and the user's recent company lookups. A user with no contacts costs one.

- **Per company in the mirror**: how many people the user knows there, with their names and
  positions. A contact is resolved as the page is read (its stored company, else whatever the
  registry has now), so a company registered a moment ago shows at once, before `link.rematch`
  has written the link.
- **Unmatched companies**, most people first: the companies contacts name that the mirror
  does not have. Spellings of one company are one row, under its commonest spelling.
- **The lookup.** `start_lookup` hands the next `LOOKUP_BATCH` unmatched company names to the
  existing `company_discovery` run (`jobs.pipeline.start_names`), which registers the ones
  whose boards it can read. Only the user starts it, from the page. It sends company names to
  the job boards Foray reads (and to web search when a key is set), never a person's name. A
  name a lookup of the last `LOOKUP_MEMORY` already tried is not tried again, so pressing the
  button twice works through the list instead of repeating its head.
- **Search**: `query` filters the contacts already read: every word of it in the name, the
  company, the position or the school, or the whole of it naming a company (`_search`).

Lists are capped (`*_SHOWN`); the counts beside them are not.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Final

from django.db.models import Count
from django.db.models.functions import Now

from apps.contacts.link import NOT_A_COMPANY, CompanyIndex, CompanyRef, company_key, load_index
from apps.contacts.models import Contact
from apps.core.errors import ApiError
from apps.core.router import ApiModel
from apps.jobs import pipeline as company_pipeline
from apps.jobs.models import Job
from apps.runs.catalogue import ACTIVE, RunKind, RunStatus
from apps.runs.models import Run

__all__ = [
    "LOOKUP_BATCH",
    "NOTHING_TO_LOOK_UP",
    "ContactsPage",
    "contacts_page",
    "start_lookup",
]

COMPANIES_SHOWN: Final = 300
PEOPLE_SHOWN: Final = 25  # per company in the mirror
UNMATCHED_SHOWN: Final = 100
UNMATCHED_PEOPLE: Final = 5
RESULTS_SHOWN: Final = 50
QUERY_MAX: Final = 100

# How many company names one lookup takes, and how long a name stays tried.
LOOKUP_BATCH: Final = 20
LOOKUP_MEMORY: Final = timedelta(days=30)

_NOT_WORD = re.compile(r"[\W_]+")

NOTHING_TO_LOOK_UP: Final = (
    "Every company your contacts name is on Foray's list or has been looked up already."
)


class ContactPerson(ApiModel):
    id: uuid.UUID
    name: str
    position: str


class ContactCompany(ApiModel):
    slug: str
    name: str
    people_count: int
    open_postings: int
    people: list[ContactPerson]  # by name, at most 25


class ContactsUnmatched(ApiModel):
    name: str  # the commonest spelling among the contacts who name it
    people_count: int
    people: list[ContactPerson]  # by name, at most 5
    looked_up: bool  # a lookup of the last 30 days tried it and registered no board


class ContactResult(ApiModel):
    id: uuid.UUID
    name: str
    position: str
    company_name: str
    company_slug: str | None  # the registered company, when it is one
    email: str
    profile_url: str
    connected_on: date | None


class ContactsLookup(ApiModel):
    names: list[str]  # what the next lookup would ask about, most people first
    remaining: int  # unmatched companies no lookup has tried, these included
    run_id: uuid.UUID | None  # a lookup in flight


class ContactsPage(ApiModel):
    total: int
    matched: int  # people at a company in the mirror
    without_company: int  # people whose row names no company
    companies: list[ContactCompany]  # most people first, at most 300
    companies_total: int
    unmatched: list[ContactsUnmatched]  # most people first, at most 100
    unmatched_total: int
    lookup: ContactsLookup
    query: str
    results: list[ContactResult] | None  # null without a query; at most 50
    results_total: int


# --- reading -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Row:
    id: uuid.UUID
    name: str
    position: str
    company_name: str
    company_key: str
    company_id: uuid.UUID | None
    email: str
    profile_url: str
    school: str
    connected_on: date | None


_COLUMNS: Final = (
    "id",
    "name",
    "position",
    "company_name",
    "company_key",
    "company_id",
    "email",
    "profile_url",
    "school",
    "connected_on",
)


@dataclass(slots=True)
class _Unmatched:
    people: list[_Row] = field(default_factory=list)
    spellings: Counter[str] = field(default_factory=Counter)

    @property
    def name(self) -> str:
        return self.spellings.most_common(1)[0][0]


@dataclass(slots=True)
class _Survey:
    """The user's contacts, sorted into the companies they are at."""

    rows: list[_Row] = field(default_factory=list)
    company_of: dict[uuid.UUID, CompanyRef] = field(default_factory=dict)  # by contact id
    matched: dict[uuid.UUID, list[_Row]] = field(default_factory=dict)  # by company id
    unmatched: dict[str, _Unmatched] = field(default_factory=dict)  # by `_group_id`
    without_company: int = 0


def _group_id(name: str) -> str:
    """What makes two spellings one unmatched company: the name's key, or for a name with no
    key (no Latin letters) the name itself."""
    return company_key(name) or f"={' '.join(name.split()).casefold()}"


def _survey(user_id: str, index: CompanyIndex | None = None) -> _Survey:
    survey = _Survey(
        rows=[
            _Row(*values)
            for values in Contact.objects.for_user(user_id)
            .order_by("name", "id")
            .values_list(*_COLUMNS)
        ]
    )
    if not survey.rows:
        return survey
    index = index or load_index()
    for row in survey.rows:
        company = index.by_id.get(row.company_id) if row.company_id is not None else None
        company = company or index.find(row.company_key, row.email)
        if company is not None:
            survey.company_of[row.id] = company
            survey.matched.setdefault(company.id, []).append(row)
        elif row.company_name:
            group = survey.unmatched.setdefault(_group_id(row.company_name), _Unmatched())
            group.people.append(row)
            group.spellings[row.company_name] += 1
        else:
            survey.without_company += 1
    return survey


def _open_postings(company_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not company_ids:
        return {}
    counts = (
        Job.objects.filter(company_id__in=company_ids, is_active=True)
        .values_list("company_id")
        .annotate(open=Count("id"))
        .order_by()
    )
    return dict(counts)


@dataclass(slots=True)
class _Lookups:
    tried: set[str] = field(default_factory=set)  # group ids a lookup has taken, or is taking
    finished: set[str] = field(default_factory=set)  # those whose lookup is over
    run_id: uuid.UUID | None = None  # the newest lookup still in flight


def _lookups(user_id: str) -> _Lookups:
    """The company names the user's discovery runs of the last month asked about. A run that
    failed or was stopped tried nothing."""
    runs = (
        Run.objects.for_user(user_id)
        .filter(kind=RunKind.COMPANY_DISCOVERY, created_at__gte=Now() - LOOKUP_MEMORY)
        .exclude(status__in=(RunStatus.FAILED, RunStatus.CANCELLED))
        .order_by("created_at")
        .values_list("id", "status", "input")
    )
    found = _Lookups()
    for run_id, status, stored in runs:
        value: dict[str, Any] = stored if isinstance(stored, dict) else {}
        listed = value.get("names")
        names: list[Any] = listed if isinstance(listed, list) else [value.get("query")]
        ids = {_group_id(name) for name in names if isinstance(name, str) and name.strip()}
        found.tried |= ids
        if status in ACTIVE:
            found.run_id = run_id
        else:
            found.finished |= ids
    return found


def _ranked(survey: _Survey) -> list[tuple[str, _Unmatched]]:
    """Unmatched companies, most people first, then by name."""
    return sorted(
        survey.unmatched.items(),
        key=lambda item: (-len(item[1].people), item[1].name.casefold()),
    )


def _lookup_candidates(survey: _Survey, lookups: _Lookups) -> list[str]:
    """Every unmatched company no lookup has tried, as the name to ask about, in rank order.
    What people write when they name no employer ("Self-employed") is never asked about."""
    return [
        group.name
        for group_id, group in _ranked(survey)
        if group_id not in lookups.tried and group_id not in NOT_A_COMPANY
    ]


# --- the page ------------------------------------------------------------------------------------


def _person(row: _Row) -> ContactPerson:
    return ContactPerson(id=row.id, name=row.name, position=row.position)


def _fold(text: str) -> str:
    """Text as the search compares it: case and punctuation aside."""
    return _NOT_WORD.sub(" ", text.casefold()).strip()


def _search(survey: _Survey, asked: str) -> list[_Row]:
    """The contacts a query finds: every word of it in the name, company, position or school,
    or the whole of it naming the company they work at, however their file spelled it ("Stripe"
    finds the people at "Stripe, Inc.")."""
    words = _fold(asked).split()
    if not words:
        return []
    key = company_key(asked)
    named = {
        company.id
        for company in set(survey.company_of.values())
        if key and company_key(company.name) == key
    }

    def found(row: _Row) -> bool:
        company = survey.company_of.get(row.id)
        if key and (row.company_key == key or (company is not None and company.id in named)):
            return True
        text = _fold(f"{row.name}\n{row.company_name}\n{row.position}\n{row.school}")
        return all(word in text for word in words)

    return [row for row in survey.rows if found(row)]


def contacts_page(user_id: str, query: str | None = None) -> ContactsPage:
    asked = " ".join((query or "").split())[:QUERY_MAX]
    survey = _survey(user_id)
    if not survey.rows:
        return ContactsPage(
            total=0,
            matched=0,
            without_company=0,
            companies=[],
            companies_total=0,
            unmatched=[],
            unmatched_total=0,
            lookup=ContactsLookup(names=[], remaining=0, run_id=None),
            query=asked,
            results=[] if asked else None,
            results_total=0,
        )

    by_company = sorted(
        survey.matched.items(),
        key=lambda item: (-len(item[1]), survey.company_of[item[1][0].id].name.casefold()),
    )[:COMPANIES_SHOWN]
    open_postings = _open_postings([company_id for company_id, _ in by_company])
    lookups = _lookups(user_id)
    candidates = _lookup_candidates(survey, lookups)

    results: list[ContactResult] | None = None
    results_total = 0
    if asked:
        found = _search(survey, asked)
        results_total = len(found)
        results = [
            ContactResult(
                id=row.id,
                name=row.name,
                position=row.position,
                company_name=row.company_name,
                company_slug=survey.company_of[row.id].slug
                if row.id in survey.company_of
                else None,
                email=row.email,
                profile_url=row.profile_url,
                connected_on=row.connected_on,
            )
            for row in found[:RESULTS_SHOWN]
        ]

    return ContactsPage(
        total=len(survey.rows),
        matched=sum(len(people) for people in survey.matched.values()),
        without_company=survey.without_company,
        companies=[
            ContactCompany(
                slug=survey.company_of[people[0].id].slug,
                name=survey.company_of[people[0].id].name,
                people_count=len(people),
                open_postings=open_postings.get(company_id, 0),
                people=[_person(row) for row in people[:PEOPLE_SHOWN]],
            )
            for company_id, people in by_company
        ],
        companies_total=len(survey.matched),
        unmatched=[
            ContactsUnmatched(
                name=group.name,
                people_count=len(group.people),
                people=[_person(row) for row in group.people[:UNMATCHED_PEOPLE]],
                looked_up=group_id in lookups.finished,
            )
            for group_id, group in _ranked(survey)[:UNMATCHED_SHOWN]
        ],
        unmatched_total=len(survey.unmatched),
        lookup=ContactsLookup(
            names=candidates[:LOOKUP_BATCH], remaining=len(candidates), run_id=lookups.run_id
        ),
        query=asked,
        results=results,
        results_total=results_total,
    )


# --- the lookup ----------------------------------------------------------------------------------


def start_lookup(user_id: str) -> tuple[company_pipeline.Started, list[str]]:
    """Starts a `company_discovery` run for the next unmatched companies (see the module
    docstring) and returns it with the names it took. 400 when none is left to try; the run's
    own throttle (`company-add:<user>`) applies."""
    names = _lookup_candidates(_survey(user_id), _lookups(user_id))[:LOOKUP_BATCH]
    if not names:
        raise ApiError(400, NOTHING_TO_LOOK_UP)
    return company_pipeline.start_names(user_id, names), names
