"""Importing contacts (#63): who is linked to which registered company, what a second import
changes, and what an import never does."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from apps.contacts import importer, link
from apps.contacts.importer import contact_key, import_contacts, import_message
from apps.contacts.link import company_key, email_domains, load_index, rematch
from apps.contacts.models import Contact
from apps.contacts.parse import ParsedContact
from apps.core import http
from apps.llm.models import LlmCall
from apps.runs.models import Event, Run
from tests.factories import make_company, make_contact, make_user
from tests.queue import deferred

pytestmark = pytest.mark.django_db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "contacts"
LINKEDIN = (FIXTURES / "linkedin-connections.csv").read_bytes()
ALUMNI = (FIXTURES / "alumni.csv").read_bytes()


def rows(user: Any) -> dict[str, Contact]:
    return {row.name: row for row in Contact.objects.for_user(user.pk)}


def snapshot(user: Any) -> list[tuple[Any, ...]]:
    """Every column of every contact, `updated_at` included."""
    names = [field.attname for field in Contact._meta.concrete_fields]
    return sorted(Contact.objects.for_user(user.pk).values_list(*names))


# --- keys and names ------------------------------------------------------------------------------


def test_company_names_compare_as_the_mirror_compares_them() -> None:
    assert company_key("Stripe") == company_key("Stripe, Inc.") == company_key("STRIPE LLC")
    assert company_key("Acme Robotics") == company_key("acme-robotics") == "acmerobotics"
    assert company_key("Johnson & Johnson") == company_key("Johnson and Johnson")
    assert company_key("株式会社") == ""  # no Latin letters: matches nothing by name
    assert company_key("") == ""


def test_a_work_email_names_its_domain_and_free_mail_names_nothing() -> None:
    assert email_domains("ada@eng.initech.example") == ["eng.initech.example", "initech.example"]
    assert email_domains("Ada@Initech.Example") == ["initech.example"]
    assert email_domains("ada@gmail.com") == []
    assert email_domains("ada@mail.yahoo.co.uk") == []
    assert email_domains("not an address") == []
    assert email_domains("") == []


def test_a_person_is_their_profile_then_their_email_then_their_name_and_company() -> None:
    by_profile = ParsedContact(name="Priya Rao", url="https://www.linkedin.com/in/priya-rao/")
    assert contact_key(by_profile) == contact_key(
        ParsedContact(
            name="Priya R.",
            company="Elsewhere",
            url="http://linkedin.com/in/Priya-Rao?trk=people",
        )
    )
    by_email = ParsedContact(name="Priya Rao", email="priya@initech.example")
    assert contact_key(by_email) == contact_key(
        ParsedContact(name="P. Rao", email="priya@initech.example", url="https://initech.example/")
    )
    assert contact_key(by_email) != contact_key(by_profile)
    # A company's own address in a list is nobody's identity.
    site = "https://initech.example/team"
    assert contact_key(ParsedContact(name="Ada", company="Initech", url=site)) != contact_key(
        ParsedContact(name="Ben", company="Initech", url=site)
    )
    by_name = ParsedContact(name="Priya  Rao", company="Stripe, Inc.")
    assert contact_key(by_name) == contact_key(ParsedContact(name="priya rao", company="Stripe"))
    assert contact_key(by_name) != contact_key(ParsedContact(name="Priya Rao", company="Linear"))


# --- the import ----------------------------------------------------------------------------------


def test_an_import_links_each_person_to_the_company_they_work_at() -> None:
    user = make_user()
    stripe = make_company(name="Stripe", slug="stripe")
    linear = make_company(name="Linear", slug="linear", source_type="ashby")
    # Registered under a shorter name: only the work email's domain ties Marcus to it.
    northwind = make_company(
        name="Northwind", slug="northwind", domain="northwind-robotics.example"
    )

    result = import_contacts(user.pk, LINKEDIN)
    assert (result.source, result.read, result.added, result.updated, result.unchanged) == (
        "linkedin",
        8,  # Lena is in the file twice
        8,
        0,
        0,
    )
    assert (result.skipped, result.total, result.matched) == (1, 8, 4)

    people = rows(user)
    assert people["Priya Rao"].company_id == stripe.id
    assert people["Tomasz Zielinski"].company_id == stripe.id  # "Stripe, Inc."
    assert people["Aiko Tanaka"].company_id == linear.id
    assert people["Marcus Bell"].company_id == northwind.id
    # Unmatched people are kept, with what the file said.
    lena = people["Lena Fischer"]
    assert (lena.company_id, lena.company_name, lena.company_key) == (
        None,
        "Globex Analytics",
        "globexanalytics",
    )
    assert lena.position == "Senior Data Scientist"  # the later of her two rows
    assert people["Ines Ortega"].company_id is None and people["Ines Ortega"].company_name == ""
    priya = people["Priya Rao"]
    assert (priya.source, priya.connected_on, priya.profile_url) == (
        "linkedin",
        date(2024, 3, 15),
        "https://www.linkedin.com/in/priya-rao-example",
    )


def test_importing_the_same_file_again_changes_nothing() -> None:
    user = make_user()
    make_company(name="Stripe", slug="stripe")
    first = import_contacts(user.pk, LINKEDIN)
    before = snapshot(user)

    again = import_contacts(user.pk, LINKEDIN)
    assert (again.added, again.updated, again.unchanged) == (0, 0, 8)
    assert (again.total, again.matched) == (first.total, first.matched)
    assert snapshot(user) == before  # not a row, not an `updated_at`
    assert import_message(again) == (
        "Nothing new: all 8 people in that file were already here. 1 row with no name was left out."
    )


def test_a_newer_export_updates_who_it_knows_and_adds_who_it_does_not() -> None:
    user = make_user()
    stripe = make_company(name="Stripe", slug="stripe")
    linear = make_company(name="Linear", slug="linear", source_type="ashby")
    import_contacts(user.pk, LINKEDIN)
    untouched = rows(user)["Aiko Tanaka"].updated_at

    newer = (
        b"First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
        # Priya moved to Linear; Tomasz no longer shares his email; Hana is new.
        b"Priya,Rao,https://www.linkedin.com/in/priya-rao-example,,Linear,Principal Engineer,"
        b"15 Mar 2024\n"
        b"Tomasz,Zielinski,https://www.linkedin.com/in/tomasz-zielinski-example,,"
        b'"Stripe, Inc.",Engineering Manager,09 Sep 2021\n'
        b"Hana,Sato,https://www.linkedin.com/in/hana-sato-example,,Stripe,Engineer,01 Nov 2026\n"
    )
    result = import_contacts(user.pk, newer)
    assert (result.added, result.updated, result.unchanged, result.total) == (1, 1, 1, 9)
    assert import_message(result) == (
        "Added 1 person and updated 1 person. 1 person already here did not change."
    )

    people = rows(user)
    priya = people["Priya Rao"]
    assert (priya.company_id, priya.company_name, priya.position) == (
        linear.id,
        "Linear",
        "Principal Engineer",
    )
    assert people["Hana Sato"].company_id == stripe.id
    # An empty column in the newer file erases nothing.
    assert people["Tomasz Zielinski"].email == "tomasz.zielinski@example.com"
    assert people["Aiko Tanaka"].updated_at == untouched
    assert "Lena Fischer" in people  # nobody is removed by an import


def test_two_lists_of_the_same_people_meet_on_the_profile_address() -> None:
    user = make_user()
    make_company(name="Linear", slug="linear", source_type="ashby")
    import_contacts(user.pk, ALUMNI)
    noor = rows(user)["Noor Haddad"]
    assert (noor.source, noor.school) == ("csv", "Westfield State University")

    linkedin = (
        b"First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
        b"Noor,Haddad,https://www.linkedin.com/in/noor-haddad-example,,Linear,Senior Engineer,"
        b"20 Aug 2025\n"
    )
    result = import_contacts(user.pk, linkedin)
    assert (result.added, result.updated) == (0, 1)
    noor = rows(user)["Noor Haddad"]
    assert (noor.source, noor.position, noor.connected_on) == (
        "linkedin",
        "Senior Engineer",
        date(2025, 8, 20),
    )
    assert noor.school == "Westfield State University"  # kept from the alumni list
    assert noor.email == "noor.haddad@gmail.com"


def test_a_free_mail_address_never_links_anyone() -> None:
    user = make_user()
    # A registry row that (wrongly) carries a mail provider's domain must not collect everyone.
    make_company(name="Mailhost", slug="mailhost", domain="gmail.com")
    import_contacts(user.pk, b"Name,Email\nAda Okafor,ada@gmail.com\n")
    assert rows(user)["Ada Okafor"].company_id is None


def test_an_invalid_board_is_not_matched_and_an_active_board_wins() -> None:
    user = make_user()
    make_company(name="Initech Labs", slug="initech-old", status="invalid")
    paused = make_company(name="Globex", slug="globex-paused", status="paused")
    page = make_company(name="Hooli", slug="hooli-page", source_type="careers_page")
    board = make_company(name="Hooli", slug="hooli")
    import_contacts(user.pk, b"Name,Company\nAda,Initech Labs\nBen,Globex\nCy,Hooli Inc.\n")
    people = rows(user)
    assert people["Ada"].company_id is None
    assert people["Ben"].company_id == paused.id  # paused is still in the mirror
    assert people["Cy"].company_id == board.id != page.id


def test_a_companys_slug_matches_when_its_name_does_not() -> None:
    user = make_user()
    company = make_company(name="Acme (YC W24)", slug="acme-robotics")
    import_contacts(user.pk, b"Name,Company\nAda,Acme Robotics\n")
    assert rows(user)["Ada"].company_id == company.id


# --- companies registered later ------------------------------------------------------------------


def test_contacts_are_linked_when_their_company_is_registered_later() -> None:
    user, other = make_user(), make_user()
    import_contacts(user.pk, LINKEDIN)
    import_contacts(other.pk, LINKEDIN)
    assert rematch() == 0  # nothing registered yet
    assert rows(user)["Lena Fischer"].company_id is None

    globex = make_company(name="Globex Analytics", slug="globex-analytics")
    assert rematch(user.pk) == 2  # Lena and Omar ("Globex Analytics, LLC")
    assert rows(user)["Lena Fischer"].company_id == globex.id
    assert rows(user)["Omar Haddad"].company_id == globex.id
    assert rows(other)["Lena Fischer"].company_id is None  # one user's was asked for

    assert rematch() == 2  # everyone's
    assert rows(other)["Lena Fischer"].company_id == globex.id
    assert rematch() == 0


def test_an_import_also_links_people_imported_before_their_company_was() -> None:
    user = make_user()
    import_contacts(user.pk, LINKEDIN)
    globex = make_company(name="Globex Analytics", slug="globex-analytics")
    result = import_contacts(user.pk, b"Name,Company\nAda Okafor,Stripe\n")
    assert result.matched == 2
    assert rows(user)["Lena Fischer"].company_id == globex.id


def test_the_registry_is_read_once_however_many_people_a_file_has(
    django_assert_max_num_queries: Any,
) -> None:
    user = make_user()
    for n in range(5):
        make_company(name=f"Company {n}", slug=f"co-{n}")
    lines = "".join(f"Person {n},Company {n % 7}\n" for n in range(300))
    # Savepoint, stored people, insert, unlinked check and read, links, totals: none per row.
    with django_assert_max_num_queries(14):
        result = import_contacts(user.pk, f"Name,Company\n{lines}".encode())
    assert (result.added, result.matched) == (300, 215)
    assert len(load_index().by_id) == 5


# --- whose contacts they are, and where they go --------------------------------------------------


def test_contacts_belong_to_the_user_who_imported_them() -> None:
    a, b = make_user(), make_user()
    import_contacts(a.pk, LINKEDIN)
    import_contacts(b.pk, ALUMNI)
    assert set(rows(a)) & set(rows(b)) == set()
    assert Contact.objects.for_user(b.pk).count() == 3

    assert importer.delete_one(b.pk, rows(a)["Priya Rao"].id) is False
    assert importer.delete_all(b.pk) == 3
    assert Contact.objects.for_user(a.pk).count() == 8
    assert importer.delete_one(a.pk, rows(a)["Priya Rao"].id) is True
    assert "Priya Rao" not in rows(a)


def test_an_import_sends_nothing_anywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    """No request, no job, no run, no event, no model call: the rows are all an import makes."""

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("an import opened an HTTP client")

    monkeypatch.setattr(http.HttpClient, "__init__", refuse)
    user = make_user()
    make_company(name="Stripe", slug="stripe")
    import_contacts(user.pk, LINKEDIN)
    import_contacts(user.pk, LINKEDIN)
    assert deferred() == []
    assert not Run.objects.for_user(user.pk).exists()
    assert not Event.objects.for_user(user.pk).exists()
    assert not LlmCall.objects.for_user(user.pk).exists()


def test_a_factory_contact_is_linked_like_an_imported_one() -> None:
    user = make_user()
    stripe = make_company(name="Stripe", slug="stripe")
    contact = make_contact(user, name="Ada Okafor", company=stripe)
    assert (contact.company_name, contact.company_key) == ("Stripe", "stripe")
    assert link.load_index().find(contact.company_key) == link.CompanyRef(
        stripe.id, "Stripe", "stripe"
    )
