"""Who the user knows at the company behind a posting (#64). Sync.

A posting is at a company where the user knows someone when one of their contacts

- is linked to the posting's registered company (`contacts.company`, set at import or by
  `link.rematch`), or
- names a company whose key is the key of the posting's own company name (`link.company_key`).
  That covers an aggregator's posting, which has no registered company, and a contact whose
  company was registered a moment ago and is not linked yet.

`known_at` answers for any number of postings in one query. People come most recently connected
first, then by name, so the few a row or a notification has room for are the ones the user is
likeliest to remember.

Every caller shows the people to their owner only, or counts them. A name or a position on its
way to a model goes through `apps/llm/untrusted.py`, like any other text a file supplied.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from django.db.models import F, Q

from apps.contacts.link import company_key
from apps.contacts.models import Contact
from apps.core.router import ApiModel
from apps.jobs.models import Job
from apps.matching.models import JobMatch

__all__ = [
    "PAGE_SHOWN",
    "ROW_SHOWN",
    "KnownPerson",
    "MatchKnows",
    "MatchPeople",
    "MatchPerson",
    "count_matches_with_contacts",
    "known_at",
    "known_at_jobs",
    "match_people",
    "row_knows",
]

ROW_SHOWN: Final = 3  # people a match's row, a notice or a tooltip names
PAGE_SHOWN: Final = 8  # people a match's page lists

type Posting = tuple[str, uuid.UUID | None, str]  # job id, company id, company name


@dataclass(frozen=True, slots=True)
class KnownPerson:
    id: uuid.UUID
    name: str
    position: str
    profile_url: str = ""


def known_at(user_id: str, postings: Iterable[Posting]) -> dict[str, list[KnownPerson]]:
    """For each posting where the user knows someone, by job id: those people, most recently
    connected first. Postings where they know nobody are left out. One query; none when there
    is no posting."""
    wanted = [(job_id, company_id, company_key(name)) for job_id, company_id, name in postings]
    company_ids = {company_id for _, company_id, _ in wanted if company_id is not None}
    keys = {key for _, _, key in wanted if key}
    if not company_ids and not keys:
        return {}
    rows = (
        Contact.objects.for_user(user_id)
        .filter(Q(company_id__in=company_ids) | Q(company_key__in=keys))
        .order_by(F("connected_on").desc(nulls_last=True), "name", "id")
        .values_list("id", "name", "position", "profile_url", "company_id", "company_key")
    )
    by_company: dict[uuid.UUID, list[KnownPerson]] = {}
    by_key: dict[str, list[KnownPerson]] = {}
    order: dict[uuid.UUID, int] = {}
    for index, (contact_id, name, position, profile_url, company_id, key) in enumerate(rows):
        person = KnownPerson(contact_id, name, position, profile_url)
        order[contact_id] = index
        if company_id is not None:
            by_company.setdefault(company_id, []).append(person)
        if key:
            by_key.setdefault(key, []).append(person)
    if not order:
        return {}

    found: dict[str, list[KnownPerson]] = {}
    merged: dict[tuple[uuid.UUID | None, str], list[KnownPerson]] = {}
    for job_id, company_id, key in wanted:
        if (company_id, key) not in merged:
            people = {
                person.id: person
                for person in (
                    *(by_company.get(company_id, ()) if company_id is not None else ()),
                    *by_key.get(key, ()),
                )
            }
            merged[(company_id, key)] = sorted(people.values(), key=lambda p: order[p.id])
        if merged[(company_id, key)]:
            found[job_id] = merged[(company_id, key)]
    return found


def known_at_jobs(user_id: str, jobs: Iterable[Job]) -> dict[str, list[KnownPerson]]:
    """`known_at` for loaded postings."""
    return known_at(user_id, [(job.id, job.company_id, job.company_name) for job in jobs])


def count_matches_with_contacts(user_id: str, match_ids: Sequence[object]) -> int:
    """How many of these matches of the user's are at a company where they know someone: what
    a watch says about the matches it announces. Two queries; none without a match."""
    ids = [str(match_id) for match_id in match_ids]
    if not ids:
        return 0
    postings = list(
        JobMatch.objects.for_user(user_id)
        .filter(id__in=ids)
        .values_list("job_id", "job__company_id", "job__company_name")
    )
    return len(known_at(user_id, postings))


# --- what the API says ---------------------------------------------------------------------------


class MatchPerson(ApiModel):
    id: uuid.UUID
    name: str
    position: str
    profile_url: str  # "" when the file gave none


# The few people a match's row has room for, and how many there are in all.
class MatchKnows(ApiModel):
    count: int
    people: list[MatchPerson]  # at most 3


# Who the match page lists under "People you know here".
class MatchPeople(ApiModel):
    count: int
    people: list[MatchPerson]  # at most 8


def _out(person: KnownPerson) -> MatchPerson:
    return MatchPerson(
        id=person.id, name=person.name, position=person.position, profile_url=person.profile_url
    )


def row_knows(people: Sequence[KnownPerson]) -> MatchKnows | None:
    """What a match's row says about the people the user knows there; None for nobody."""
    if not people:
        return None
    return MatchKnows(count=len(people), people=[_out(person) for person in people[:ROW_SHOWN]])


def match_people(user_id: str, job: Job) -> MatchPeople:
    """Who the user knows at one posting's company, for its match page. One query."""
    people = known_at_jobs(user_id, [job]).get(job.id, [])
    return MatchPeople(count=len(people), people=[_out(person) for person in people[:PAGE_SHOWN]])
