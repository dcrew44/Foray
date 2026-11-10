"""Importing a contacts file, and removing contacts (#63). Sync.

`import_contacts(user_id, data)` reads the file (`parse.parse_contacts`), links each person to
the registered company they work at (`link.CompanyIndex`) and writes the result in one
transaction. It makes no request and defers no job: a contact's row leaves the database only
in the pages its owner reads.

**The same person twice is one row.** `contact_key` is who a person is: their LinkedIn profile
address when the file has one, else their email, else their name and company. So:

- importing the same file again changes nothing (no row is written, no `updated_at` moves);
- a newer export updates the people it already knows (a new employer, a new title) and adds
  the new ones. A value the newer file leaves empty keeps the stored one, so an export without
  emails does not erase them;
- nobody is removed by an import. `delete_all` and `delete_one` are the only ways out.

A person known only by name and company who changes employer is a new row, since nothing ties
the two together; LinkedIn's export always carries the profile address, so this is only true of
hand-made lists.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from django.db import transaction
from django.db.models import Count, Q
from django.db.models.functions import Now

from apps.contacts.link import CompanyIndex, company_key, load_index, rematch
from apps.contacts.models import Contact, ContactSource
from apps.contacts.parse import ParsedContact, parse_contacts
from apps.jobs.sources.dedupe import sha1_hex

__all__ = [
    "ImportResult",
    "contact_key",
    "delete_all",
    "delete_one",
    "import_contacts",
    "import_message",
]

BATCH = 1000
# Stored people are looked up by key this many at a time: a statement takes 65,535 parameters.
LOOKUP_BATCH = 5000

_SCHEME = re.compile(r"^https?://(?:www\.)?", re.IGNORECASE)
_PROFILE = re.compile(r"^(?:[a-z]{2,3}\.)?linkedin\.com/(?:in|pub)/[^/?#]+", re.IGNORECASE)
_SPACES = re.compile(r"\s+")

# What an import may change on a person it already knows.
_FIELDS = (
    "name",
    "company_name",
    "company_key",
    "position",
    "email",
    "profile_url",
    "school",
    "connected_on",
    "source",
    "company_id",
)


@dataclass(frozen=True, slots=True)
class ImportResult:
    source: str  # which kind of file it was
    read: int  # people in the file, the same person counted once
    added: int
    updated: int
    unchanged: int
    skipped: int  # rows that named nobody
    total: int  # the user's contacts after the import
    matched: int  # of those, at a company in the mirror


def contact_key(person: ParsedContact) -> str:
    """Who this person is across imports (see the module docstring). Only a LinkedIn profile
    address counts as an identity: any other address in a list may be a company's."""
    address = _SCHEME.sub("", person.url)
    profile = _PROFILE.match(address)
    if profile is not None:
        basis = f"url:{profile.group().lower()}"
    elif person.email:
        basis = f"email:{person.email}"
    else:
        name = _SPACES.sub(" ", person.name).strip().casefold()
        basis = f"name:{name}|{company_key(person.company) or person.company.casefold()}"
    return sha1_hex(basis)


def _merged(stored: Contact, person: ParsedContact, source: str, index: CompanyIndex) -> bool:
    """Writes what the file says onto a stored contact; True when anything changed."""
    before = tuple(getattr(stored, name) for name in _FIELDS)
    stored.name = person.name
    if person.company and person.company != stored.company_name:
        stored.company_name = person.company
        stored.company_key = company_key(person.company)
        stored.company_id = None  # a new employer: linked again below
    stored.position = person.position or stored.position
    stored.email = person.email or stored.email
    stored.profile_url = person.url or stored.profile_url
    stored.school = person.school or stored.school
    stored.connected_on = person.connected_on or stored.connected_on
    if source == ContactSource.LINKEDIN:
        stored.source = source
    if stored.company_id is None:
        company = index.find(stored.company_key, stored.email)
        stored.company_id = company.id if company is not None else None
    return tuple(getattr(stored, name) for name in _FIELDS) != before


def _new(person: ParsedContact, key: str, source: str, index: CompanyIndex) -> Contact:
    name_key = company_key(person.company)
    company = index.find(name_key, person.email)
    return Contact(
        name=person.name,
        company_name=person.company,
        company_key=name_key,
        position=person.position,
        email=person.email,
        profile_url=person.url,
        school=person.school,
        connected_on=person.connected_on,
        source=source,
        dedupe_key=key,
        company_id=company.id if company is not None else None,
    )


def _totals(user_id: str) -> tuple[int, int]:
    counts = Contact.objects.for_user(user_id).aggregate(
        total=Count("id"), matched=Count("id", filter=Q(company__isnull=False))
    )
    return counts["total"], counts["matched"]


def import_contacts(user_id: str, data: bytes) -> ImportResult:
    """Reads a contacts file into the user's contacts. Raises `parse.Unreadable` for a file
    that is not a list of people, before anything is written."""
    parsed = parse_contacts(data)
    people: dict[str, ParsedContact] = {}
    for person in parsed.contacts:
        people[contact_key(person)] = person  # the same person twice: the later row

    index = load_index()
    keys = list(people)
    with transaction.atomic():
        stored: dict[str, Contact] = {}
        for start in range(0, len(keys), LOOKUP_BATCH):
            found = Contact.objects.for_user(user_id).filter(
                dedupe_key__in=keys[start : start + LOOKUP_BATCH]
            )
            stored.update((row.dedupe_key, row) for row in found)
        new: list[Contact] = []
        changed: list[Contact] = []
        for key, person in people.items():
            row = stored.get(key)
            if row is None:
                new.append(_new(person, key, parsed.source, index))
            elif _merged(row, person, parsed.source, index):
                row.updated_at = Now()
                changed.append(row)
        if new:
            # Another import of the same file may be running: the constraint has the last word.
            Contact.objects.bulk_create_for(user_id, new, batch_size=BATCH, ignore_conflicts=True)
        if changed:
            Contact.objects.for_user(user_id).bulk_update(
                changed, [*_FIELDS, "updated_at"], batch_size=BATCH
            )
        # People imported before their company was registered.
        rematch(user_id, index=index)
        total, matched = _totals(user_id)
    return ImportResult(
        source=parsed.source,
        read=len(people),
        added=len(new),
        updated=len(changed),
        unchanged=len(people) - len(new) - len(changed),
        skipped=parsed.skipped,
        total=total,
        matched=matched,
    )


def _people(count: int) -> str:
    return "1 person" if count == 1 else f"{count:,} people"


def import_message(result: ImportResult) -> str:
    """What the import did, in a sentence or two."""
    if result.added == 0 and result.updated == 0:
        message = (
            "Nothing new: the 1 person in that file was already here."
            if result.read == 1
            else f"Nothing new: all {result.read:,} people in that file were already here."
        )
    else:
        parts = []
        if result.added:
            parts.append(f"added {_people(result.added)}")
        if result.updated:
            parts.append(f"updated {_people(result.updated)}")
        message = " and ".join(parts).capitalize() + "."
        if result.unchanged:
            message += f" {_people(result.unchanged).capitalize()} already here did not change."
    if result.skipped == 1:
        message += " 1 row with no name was left out."
    elif result.skipped:
        message += f" {result.skipped:,} rows with no name were left out."
    return message


def delete_all(user_id: str) -> int:
    """Removes every contact of the user's. Returns how many there were."""
    with transaction.atomic():
        count = Contact.objects.for_user(user_id).count()
        Contact.objects.for_user(user_id).delete()
    return count


def delete_one(user_id: str, contact_id: uuid.UUID | str) -> bool:
    """Removes one contact; False when it is not the user's."""
    with transaction.atomic():
        found = Contact.objects.for_user(user_id).filter(id=contact_id)
        if not found.exists():
            return False
        found.delete()
    return True
