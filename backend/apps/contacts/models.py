"""People the user knows (#63; DESIGN.md, Outreach and Referrals).

A `Contact` is one person from a file the user exported themself: LinkedIn's own
"Connections.csv", or any list with a name column (an alumni list, a spreadsheet of former
colleagues). Foray never signs in to LinkedIn and never fetches a profile: the file is the only
source, and a contact's row is never sent anywhere.

- `dedupe_key` is who the person is across imports (`importer.contact_key`: their profile
  address, else their email, else their name and company), unique per user, so importing the
  same file again changes nothing and a newer export updates the people it already knows.
- `company_name` is the company as the file wrote it; `company_key` is that name normalised the
  way the mirror normalises company names (`link.company_key`), which is what postings are
  compared with.
- `company` is the registered company the contact works at, when one matched by name or by the
  domain of a work email (`link.CompanyIndex`). A contact whose company is not in the mirror
  keeps it null and is linked later, when that company is registered (`link.rematch`).

An `OutreachNote` is the short note to one contact about one match (#25): what the model
drafted, as the user has edited it since. There is one per match and contact; drafting again
replaces it. It is the user's to copy and send. Foray has no way to send it.
"""

from __future__ import annotations

import uuid

from django.db import models
from django.db.models.functions import Now

from apps.accounts.models import User
from apps.core.choices import choices_checks
from apps.core.ids import new_id
from apps.core.models import Timestamped
from apps.core.scoping import UserOwned
from apps.jobs.models import Company
from apps.matching.models import JobMatch


class ContactSource(models.TextChoices):
    LINKEDIN = "linkedin"  # LinkedIn's own export of the user's connections
    CSV = "csv"  # any other list the user uploaded


class Contact(UserOwned, Timestamped):
    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    # Indexed by the (user, dedupe_key) constraint.
    user = models.ForeignKey(User, models.CASCADE, related_name="contacts", db_index=False)
    name = models.TextField()
    company_name = models.TextField(default="")
    company_key = models.TextField(default="")
    position = models.TextField(default="")
    email = models.TextField(default="")
    profile_url = models.TextField(default="")
    school = models.TextField(default="")  # an alumni list's column; LinkedIn's export has none
    connected_on = models.DateField(null=True)
    source = models.TextField(choices=ContactSource.choices)
    dedupe_key = models.TextField()
    company = models.ForeignKey(Company, models.SET_NULL, null=True, related_name="+")

    user_id: str
    company_id: uuid.UUID | None

    class Meta(UserOwned.Meta):
        db_table = "contacts"
        constraints = [
            models.UniqueConstraint(fields=["user", "dedupe_key"], name="contacts_once_per_person"),
            *choices_checks("contacts", source=ContactSource),
        ]
        indexes = [
            models.Index(fields=["user", "company"], name="contacts_user_company"),
            models.Index(fields=["user", "company_key"], name="contacts_user_company_key"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.company_name or 'no company'})"


class OutreachNote(UserOwned, Timestamped):
    id = models.UUIDField(primary_key=True, default=new_id, editable=False)
    # Indexed by the (user, match, contact) constraint.
    user = models.ForeignKey(User, models.CASCADE, related_name="outreach_notes", db_index=False)
    match = models.ForeignKey(JobMatch, models.CASCADE, related_name="outreach_notes")
    contact = models.ForeignKey(Contact, models.CASCADE, related_name="outreach_notes")
    body = models.TextField()  # the note as it stands: the draft, then the user's edits
    drafted_at = models.DateTimeField(db_default=Now())  # when the model last wrote it
    # When the user last changed it; null while it is still what the model wrote.
    edited_at = models.DateTimeField(null=True)

    user_id: str
    match_id: uuid.UUID
    contact_id: uuid.UUID

    class Meta(UserOwned.Meta):
        db_table = "outreach_notes"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "match", "contact"], name="outreach_notes_once_per_contact"
            ),
        ]

    def __str__(self) -> str:
        return f"note {self.id} for match {self.match_id}"
