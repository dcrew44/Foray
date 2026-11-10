"""Which registered company a contact works at (#63). Sync.

A contact names its company in the file's own words ("Stripe, Inc."), and the mirror has its own
("Stripe"). They are compared the way the mirror already compares company names:
`sources.dedupe.normalize_company` (lowercased, legal suffixes dropped, punctuation folded),
then with the spaces taken out, so "Acme Robotics", "acme-robotics" and "AcmeRobotics, LLC" are
one company. That is `company_key`, stored on the contact.

`CompanyIndex` is the registry read once (one query) and looked up in memory:

1. **by name**: the contact's key equals a company's name key, or its slug's;
2. **by work email**: the domain of the contact's email equals `companies.domain`, or is a
   subdomain of it. Never for a free-mail domain (`FREE_MAIL`): a gmail.com address says
   nothing about an employer.

An `invalid` company (its board is gone) is not matched to: the contact stays unmatched, and
looking for that company's board again is what brings it back. Among several companies with one
name, an active one wins over a paused one, a board over a careers page, the oldest over the
rest.

`rematch()` links the contacts that had no company when they were imported to companies
registered since. It is cheap (nothing to do is one `EXISTS`), so it runs at every import and
before watches start (`matching.tasks.watch_fan_out`); the Contacts page resolves the same way
as it reads, so it never waits for either.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Final

from django.db.models import QuerySet
from django.db.models.functions import Now

from apps.contacts.models import Contact
from apps.jobs.models import Company, CompanyStatus, SourceType
from apps.jobs.sources.dedupe import normalize_company

__all__ = [
    "FREE_MAIL",
    "NOT_A_COMPANY",
    "CompanyIndex",
    "CompanyRef",
    "company_key",
    "email_domains",
    "load_index",
    "rematch",
]

_NOT_ALNUM = re.compile(r"[^a-z0-9]+")

# Mail providers anyone can sign up with: an address there names no employer.
FREE_MAIL: Final = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "yahoo.co.uk",
        "yahoo.co.in",
        "yahoo.ca",
        "yahoo.fr",
        "yahoo.de",
        "ymail.com",
        "rocketmail.com",
        "hotmail.com",
        "hotmail.co.uk",
        "outlook.com",
        "live.com",
        "msn.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
        "pm.me",
        "gmx.com",
        "gmx.de",
        "gmx.net",
        "web.de",
        "mail.com",
        "yandex.com",
        "yandex.ru",
        "zoho.com",
        "fastmail.com",
        "hey.com",
        "duck.com",
        "qq.com",
        "163.com",
        "126.com",
        "naver.com",
        "comcast.net",
        "verizon.net",
        "att.net",
        "sbcglobal.net",
        "cox.net",
    }
)

# What people write in the company field when they have no employer to name. These are never
# looked up as companies (`page.next_lookup`).
NOT_A_COMPANY: Final = frozenset(
    {
        "selfemployed",
        "freelance",
        "freelancer",
        "independent",
        "independentconsultant",
        "consultant",
        "stealth",
        "stealthstartup",
        "stealthmode",
        "stealthmodestartup",
        "confidential",
        "unemployed",
        "retired",
        "student",
        "none",
        "na",
        "seekingopportunities",
        "opentowork",
    }
)


def company_key(name: str) -> str:
    """A company name as contacts and postings are compared: `normalize_company`, no spaces.
    Empty for a name with no Latin letters or digits, which then matches nothing by name."""
    return _NOT_ALNUM.sub("", normalize_company(name))


def email_domains(email: str) -> list[str]:
    """The domain of a work email and its parents down to two labels (`eng.acme.com`,
    `acme.com`); none for a free-mail address or something that is not an address."""
    _, at, domain = email.strip().lower().rpartition("@")
    if not at or "." not in domain:
        return []
    labels = domain.split(".")
    candidates = [".".join(labels[start:]) for start in range(len(labels) - 1)]
    return [] if any(candidate in FREE_MAIL for candidate in candidates) else candidates


@dataclass(frozen=True, slots=True)
class CompanyRef:
    id: uuid.UUID
    name: str
    slug: str


@dataclass(slots=True)
class CompanyIndex:
    by_id: dict[uuid.UUID, CompanyRef] = field(default_factory=dict)
    by_key: dict[str, CompanyRef] = field(default_factory=dict)
    by_domain: dict[str, CompanyRef] = field(default_factory=dict)

    def find(self, key: str, email: str = "") -> CompanyRef | None:
        """The company a contact with this company key and email works at, if it is one the
        mirror has."""
        if key and key in self.by_key:
            return self.by_key[key]
        for domain in email_domains(email):
            if domain in self.by_domain:
                return self.by_domain[domain]
        return None


def load_index() -> CompanyIndex:
    """The registry as a lookup. One query; `listing_cache` and the other large columns are
    not read."""
    rows = list(
        Company.objects.values_list(
            "id", "name", "slug", "domain", "status", "source_type"
        ).order_by("created_at", "id")
    )
    # The preferred company first, so `setdefault` keeps it.
    rows.sort(key=lambda row: (row[4] != CompanyStatus.ACTIVE, row[5] == SourceType.CAREERS_PAGE))
    index = CompanyIndex()
    slugs: list[tuple[str, CompanyRef]] = []
    for company_id, name, slug, domain, status, _source_type in rows:
        ref = CompanyRef(company_id, name, slug)
        index.by_id[company_id] = ref
        if status == CompanyStatus.INVALID:
            continue
        key = company_key(name)
        if key:
            index.by_key.setdefault(key, ref)
        if domain:
            index.by_domain.setdefault(domain.lower(), ref)
        slugs.append((_NOT_ALNUM.sub("", slug.lower()), ref))
    # A name always wins over another company's slug.
    for key, ref in slugs:
        if key:
            index.by_key.setdefault(key, ref)
    return index


def _unlinked(user_id: str | None) -> QuerySet[Contact]:
    rows = (
        Contact.objects.for_user(user_id)
        if user_id is not None
        else Contact.objects.unscoped("contacts: link people to companies registered since")
    )
    return rows.filter(company__isnull=True).exclude(company_key="", email="")


def rematch(user_id: str | None = None, *, index: CompanyIndex | None = None) -> int:
    """Links contacts with no company yet to the companies the mirror has now: one user's, or
    everyone's when no user is given. Returns how many were linked."""
    if not _unlinked(user_id).exists():
        return 0
    index = index or load_index()
    found: dict[uuid.UUID, list[uuid.UUID]] = {}
    for contact_id, key, email in _unlinked(user_id).values_list("id", "company_key", "email"):
        company = index.find(key, email)
        if company is not None:
            found.setdefault(company.id, []).append(contact_id)
    linked = 0
    for company_id, contact_ids in found.items():
        linked += (
            _unlinked(user_id)
            .filter(id__in=contact_ids)
            .update(company_id=company_id, updated_at=Now())
        )
    return linked
