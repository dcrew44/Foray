"""Reading a contacts file (#63): LinkedIn's Connections.csv in both shapes it has had, the
archive it comes in, and any other list with a name column. Pure: no database.

Every person in the fixtures is made up.
"""

from __future__ import annotations

import codecs
import io
import zipfile
from datetime import date
from pathlib import Path

import pytest

from apps.contacts import parse
from apps.contacts.parse import ParsedContact, Unreadable, parse_contacts, parse_date

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "contacts"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def by_name(people: list[ParsedContact]) -> dict[str, ParsedContact]:
    return {person.name: person for person in people}


def refused(data: bytes) -> tuple[int, str]:
    with pytest.raises(Unreadable) as caught:
        parse_contacts(data)
    return caught.value.status, caught.value.message


# --- LinkedIn's export ---------------------------------------------------------------------------


def test_linkedins_export_is_read_past_its_notes() -> None:
    parsed = parse_contacts(fixture("linkedin-connections.csv"))
    assert parsed.source == "linkedin"
    # The notes, the blank line, the header and the empty row are not people; the row with a
    # company and no name is counted as skipped.
    assert [person.name for person in parsed.contacts] == [
        "Priya Rao",
        "Marcus Bell",
        "Ines Ortega",
        "Tomasz Zielinski",
        "Aiko Tanaka",
        "Devon Clarke",
        "Lena Fischer",
        "Lena Fischer",
        "Omar Haddad",
    ]
    assert parsed.skipped == 1
    people = by_name(parsed.contacts)
    assert people["Priya Rao"] == ParsedContact(
        name="Priya Rao",
        company="Stripe",
        position="Staff Engineer",
        email="",
        url="https://www.linkedin.com/in/priya-rao-example",
        school="",
        connected_on=date(2024, 3, 15),
    )


def test_quoted_commas_and_quotes_stay_inside_their_field() -> None:
    people = by_name(parse_contacts(fixture("linkedin-connections.csv")).contacts)
    marcus = people["Marcus Bell"]
    assert marcus.company == "Northwind Robotics, Inc."
    assert marcus.position == "Senior Recruiter, Engineering"
    assert marcus.email == "marcus.bell@northwind-robotics.example"
    assert people["Omar Haddad"].position == 'Data Engineer "Platform"'
    assert people["Omar Haddad"].company == "Globex Analytics, LLC"


def test_an_empty_company_is_kept_empty() -> None:
    ines = by_name(parse_contacts(fixture("linkedin-connections.csv")).contacts)["Ines Ortega"]
    assert (ines.company, ines.position, ines.email) == ("", "", "")
    assert ines.connected_on == date(2022, 11, 28)


@pytest.mark.parametrize(
    "wrap",
    [
        lambda data: codecs.BOM_UTF8 + data,
        lambda data: data.replace(b"\n", b"\r\n"),
        lambda data: codecs.BOM_UTF8 + data.replace(b"\n", b"\r\n"),
        lambda data: codecs.BOM_UTF16_LE + data.decode().encode("utf-16-le"),
    ],
    ids=["bom", "crlf", "bom-and-crlf", "utf-16"],
)
def test_a_bom_or_other_line_endings_change_nothing(wrap) -> None:  # type: ignore[no-untyped-def]
    plain = parse_contacts(fixture("linkedin-connections.csv"))
    assert parse_contacts(wrap(fixture("linkedin-connections.csv"))) == plain


def test_the_older_export_has_no_address_column_and_another_date() -> None:
    parsed = parse_contacts(fixture("linkedin-connections-2018.csv"))
    assert parsed.source == "linkedin"
    people = by_name(parsed.contacts)
    assert people["Priya Rao"].connected_on == date(2018, 3, 15)
    assert people["Marcus Bell"].connected_on == date(2017, 12, 1)
    assert people["Aiko Tanaka"].connected_on == date(2018, 1, 9)
    assert all(person.url == "" for person in parsed.contacts)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("15 Mar 2024", date(2024, 3, 15)),
        ("5 Sep 2019", date(2019, 9, 5)),
        ("15 March 2024", date(2024, 3, 15)),
        ("15-Mar-24", date(2024, 3, 15)),
        ("3/15/24, 2:30 PM", date(2024, 3, 15)),
        ("3/15/2024", date(2024, 3, 15)),
        ("12/1/17, 9:05 AM", date(2017, 12, 1)),
        ("2024-03-15", date(2024, 3, 15)),
        ("2024-03-15T10:00:00Z", date(2024, 3, 15)),
        ("", None),
        ("last spring", None),
        ("31 Feb 2024", None),
        ("13/40/2024", None),
        ("15 Foo 2024", None),
    ],
)
def test_dates_in_every_shape_linkedin_has_used(text: str, expected: date | None) -> None:
    assert parse_date(text) == expected


def test_linkedins_archive_is_read_for_its_connections_file() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Profile.csv", "First Name,Last Name\nNot,Aconnection\n")
        archive.writestr("Connections.csv", fixture("linkedin-connections.csv"))
    assert parse_contacts(buffer.getvalue()) == parse_contacts(fixture("linkedin-connections.csv"))

    without = io.BytesIO()
    with zipfile.ZipFile(without, "w") as archive:
        archive.writestr("Profile.csv", "First Name,Last Name\nNot,Aconnection\n")
    assert refused(without.getvalue()) == (400, parse.NO_CONNECTIONS_INSIDE)


def test_an_archive_that_declares_too_much_is_refused_before_it_is_inflated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Connections.csv", "First Name,Last Name\n" + "A,B\n" * 5000)
    monkeypatch.setattr(parse, "MAX_UNZIPPED", 1000)
    assert refused(buffer.getvalue())[0] == 413
    assert refused(b"PK\x03\x04 not really an archive") == (415, parse.NOT_A_LIST)


# --- any other list ------------------------------------------------------------------------------


def test_a_list_with_other_headers_and_semicolons_is_read() -> None:
    parsed = parse_contacts(fixture("alumni.csv"))
    assert parsed.source == "csv"
    assert parsed.skipped == 1  # the row with an employer and no name
    people = by_name(parsed.contacts)
    assert people["Noor Haddad"] == ParsedContact(
        name="Noor Haddad",
        company="Linear",
        position="Software Engineer",
        email="noor.haddad@gmail.com",
        url="https://linkedin.com/in/noor-haddad-example",
        school="Westfield State University",
        connected_on=None,
    )
    assert people["Sofia Lindqvist"].company == ""
    assert people["Caleb Mwangi"].email == "caleb@initechlabs.example"


def test_header_names_are_matched_leniently() -> None:
    text = "SURNAME\tGiven Name\tOrganisation\tROLE\tWork Email\tProfile URL\n"
    text += "Okafor\tAda\tInitech Labs\tDesigner\tADA@Initech.Example \twww.linkedin.com/in/ada\n"
    [ada] = parse_contacts(text.encode()).contacts
    assert ada == ParsedContact(
        name="Ada Okafor",
        company="Initech Labs",
        position="Designer",
        email="ada@initech.example",
        url="https://www.linkedin.com/in/ada",
    )


def test_a_single_name_column_is_enough() -> None:
    parsed = parse_contacts(b"Name\nAda Okafor\nBen Achterberg\n")
    assert [person.name for person in parsed.contacts] == ["Ada Okafor", "Ben Achterberg"]


def test_values_that_do_not_read_are_left_empty_not_fatal() -> None:
    text = "Name,Email,URL,Connected On\nAda Okafor,not an email,javascript:alert(1),someday\n"
    [ada] = parse_contacts(text.encode()).contacts
    assert (ada.email, ada.url, ada.connected_on) == ("", "", None)


def test_fields_are_put_on_one_line_and_cut() -> None:
    text = f'Name,Company\n"Ada\n\tOkafor\x07","{"C" * 500}"\n'
    [ada] = parse_contacts(text.encode()).contacts
    assert ada.name == "Ada Okafor"
    assert len(ada.company) == parse.LIMITS["company"]


def test_a_windows_spreadsheets_csv_is_read() -> None:
    [zoe] = parse_contacts("Name,Company\nZoë Müller,Café Löwe\n".encode("cp1252")).contacts
    assert (zoe.name, zoe.company) == ("Zoë Müller", "Café Löwe")


# --- what is not a list of people ----------------------------------------------------------------


def test_files_that_are_not_lists_of_people_are_refused_with_a_sentence() -> None:
    assert refused(b"%PDF-1.7 a resume, not a list") == (415, parse.NOT_A_LIST)
    assert refused(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR") == (415, parse.NOT_A_LIST)
    assert refused(b"") == (400, parse.EMPTY)
    assert refused(b"  \n\n") == (400, parse.EMPTY)
    assert refused(b"Company,Position\nStripe,Engineer\n") == (400, parse.NO_NAMES)
    assert refused(b"First Name,Last Name,Company\n") == (400, parse.EMPTY)
    assert refused(b"First Name,Last Name,Company\n,,Stripe\n") == (400, parse.EMPTY)


def test_a_file_with_too_many_rows_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(parse, "MAX_ROWS", 3)
    rows = "".join(f"Person {n},Stripe\n" for n in range(3))
    assert len(parse_contacts(f"Name,Company\n{rows}".encode()).contacts) == 3
    assert refused(f"Name,Company\n{rows}Person 4,Stripe\n".encode())[0] == 413
