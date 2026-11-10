"""Reading a contacts file (#63). Pure: bytes in, people out; no database, no network.

Two shapes are read, told apart by their header row:

- **LinkedIn's "Connections.csv"** (Settings & Privacy, Data privacy, Get a copy of your data,
  Connections). It opens with a few "Notes:" lines, then `First Name,Last Name,URL,Email
  Address,Company,Position,Connected On`. The email column is empty unless the connection
  chose to share it. `Connected On` has been written two ways over the years, `15 Mar 2024`
  and `3/15/24, 2:30 PM`; both are read. The archive LinkedIn sends (`.zip`) is read too: the
  `Connections.csv` inside it is what is parsed.
- **Any other CSV with a name column** (an alumni list, a spreadsheet of former colleagues).
  Header names are matched leniently, ignoring case, spaces and punctuation: a name (`Name`, or
  `First Name` and `Last Name`), and any of company, title or position, email, a profile
  address and school. Comma, semicolon and tab separated files are all read.

What is not a contact is left out quietly: lines before the header, blank rows, and rows with
no name (counted in `skipped`). A value that cannot be read (a date in another shape, an email
without an `@`) is left empty rather than failing the row. `Unreadable` carries the status and
the sentence to show for a file that is not a list of people at all.
"""

from __future__ import annotations

import codecs
import csv
import io
import itertools
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from typing import Final, Literal

__all__ = [
    "MAX_ROWS",
    "Parsed",
    "ParsedContact",
    "Unreadable",
    "parse_contacts",
    "parse_date",
]

type Source = Literal["linkedin", "csv"]

# LinkedIn caps a member at 30,000 connections; anything past this is not a contact list.
MAX_ROWS: Final = 50_000
# The export's CSV inside its archive: a few megabytes at most.
MAX_UNZIPPED: Final = 20 * 1024 * 1024
MAX_ZIP_ENTRIES: Final = 2_000
HEADER_WITHIN: Final = 30  # the header is one of the first rows; LinkedIn's is the fourth

NOT_A_LIST: Final = "Upload a CSV file: LinkedIn's Connections.csv, or any list with a name column."
NO_NAMES: Final = (
    "That file has no name column. It needs a Name column, or First Name and Last Name."
)
EMPTY: Final = "That file lists no people."
TOO_MANY: Final = f"That file has more than {MAX_ROWS:,} rows. Split it and import the parts."
NO_CONNECTIONS_INSIDE: Final = (
    "That archive has no Connections.csv in it. Ask LinkedIn for a copy of your connections, "
    "then upload the archive or the Connections.csv inside it."
)

# How long a field may be. A longer one is cut: no real name, title or address is.
LIMITS: Final = {
    "name": 200,
    "company": 200,
    "position": 300,
    "email": 320,
    "url": 500,
    "school": 200,
}

_ZIP_MAGIC: Final = b"PK\x03\x04"
_PDF_MAGIC: Final = b"%PDF-"
_DELIMITERS: Final = (",", ";", "\t", "|")
_KEY = re.compile(r"[^a-z0-9]+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_HTTP = re.compile(r"^https?://", re.IGNORECASE)
_BARE_ADDRESS = re.compile(r"^(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+/\S*$", re.IGNORECASE)

# Header names, folded to letters and digits, and the field each one is.
_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "first": ("firstname", "first", "givenname", "forename"),
    "last": ("lastname", "last", "surname", "familyname"),
    "name": ("name", "fullname", "contact", "contactname", "person"),
    "company": (
        "company",
        "companyname",
        "currentcompany",
        "employer",
        "currentemployer",
        "organization",
        "organisation",
        "organizationname",
    ),
    "position": (
        "position",
        "title",
        "jobtitle",
        "currentposition",
        "currenttitle",
        "role",
        "headline",
    ),
    "email": ("email", "emailaddress", "primaryemail", "workemail", "email1value"),
    "url": ("url", "profileurl", "profile", "linkedin", "linkedinurl", "linkedinprofile"),
    "school": ("school", "university", "college", "almamater", "education"),
    "connected": ("connectedon", "connected", "dateconnected"),
}
_FIELD_OF: Final = {alias: field for field, aliases in _ALIASES.items() for alias in aliases}

_MONTHS: Final = {
    name: number
    for number, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}
_DAY_MONTH_YEAR = re.compile(r"^(\d{1,2})[ -]([A-Za-z]{3})[A-Za-z]*\.?,?[ -](\d{2}|\d{4})$")
_MONTH_DAY_YEAR = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})(?:\D.*)?$")
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:\D.*)?$")


class Unreadable(Exception):
    """A file that is not a list of people: the status to answer and the sentence to show."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True, slots=True)
class ParsedContact:
    name: str
    company: str = ""
    position: str = ""
    email: str = ""
    url: str = ""
    school: str = ""
    connected_on: date | None = None


@dataclass(frozen=True, slots=True)
class Parsed:
    source: Source
    contacts: list[ParsedContact]
    skipped: int  # rows that named nobody


# --- bytes to text -------------------------------------------------------------------------------


def _from_archive(data: bytes) -> bytes:
    """The connections file inside LinkedIn's archive. Its declared size is checked before
    anything is inflated, and `zipfile` never inflates an entry past what it declares."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ZIP_ENTRIES:
                raise Unreadable(415, NOT_A_LIST)
            wanted = [
                entry
                for entry in entries
                if entry.filename.rsplit("/", 1)[-1].lower() == "connections.csv"
            ]
            if not wanted:
                raise Unreadable(400, NO_CONNECTIONS_INSIDE)
            if wanted[0].file_size > MAX_UNZIPPED:
                raise Unreadable(413, TOO_MANY)
            return archive.read(wanted[0])
    except zipfile.BadZipFile, zipfile.LargeZipFile, OSError, ValueError, EOFError, RuntimeError:
        # RuntimeError: an encrypted entry. None of these is a file Foray can read.
        raise Unreadable(415, NOT_A_LIST) from None


def _decode(data: bytes) -> str:
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            raise Unreadable(415, NOT_A_LIST) from None
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")  # a spreadsheet's "CSV" on Windows
    if "\x00" in text:
        raise Unreadable(415, NOT_A_LIST)  # a binary file, whatever its name
    return text


# --- text to rows --------------------------------------------------------------------------------


def _rows(text: str, delimiter: str) -> Iterator[list[str]]:
    return csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)


def _columns(row: list[str]) -> dict[str, int]:
    """Which field each recognised header cell names; the first of two columns wins."""
    found: dict[str, int] = {}
    for index, cell in enumerate(row):
        field = _FIELD_OF.get(_KEY.sub("", cell.lower()))
        if field is not None:
            found.setdefault(field, index)
    return found


def _is_header(columns: dict[str, int]) -> bool:
    """A row that names people: a name column, or both halves of one, or one half beside
    another recognised column."""
    if "name" in columns or ("first" in columns and "last" in columns):
        return True
    return ("first" in columns or "last" in columns) and len(columns) >= 2


@dataclass(frozen=True, slots=True)
class _Layout:
    delimiter: str
    header_at: int
    columns: dict[str, int]


def _layout(text: str) -> _Layout | None:
    """The delimiter and header row that recognise the most columns."""
    best: _Layout | None = None
    for delimiter in _DELIMITERS:
        try:
            for index, row in enumerate(itertools.islice(_rows(text, delimiter), HEADER_WITHIN)):
                columns = _columns(row)
                if _is_header(columns):
                    if best is None or len(columns) > len(best.columns):
                        best = _Layout(delimiter, index, columns)
                    break
        except csv.Error:
            continue
    return best


# --- rows to people ------------------------------------------------------------------------------


def _clean(value: str, limit: int) -> str:
    """One field on one line, trimmed and cut to its limit."""
    return " ".join(_CONTROL.sub(" ", value).split())[:limit].rstrip()


def _email(value: str) -> str:
    value = value.strip().lower()
    return value if len(value) <= LIMITS["email"] and _EMAIL.match(value) else ""


def _url(value: str) -> str:
    value = "".join(value.split())
    if not value or len(value) > LIMITS["url"]:
        return ""
    if _HTTP.match(value):
        return value
    return f"https://{value}" if _BARE_ADDRESS.match(value) else ""


def parse_date(value: str) -> date | None:
    """`15 Mar 2024`, `3/15/24, 2:30 PM` (month first, as LinkedIn wrote it), `2024-03-15`;
    None for anything else."""
    text = " ".join(value.split())
    try:
        if found := _DAY_MONTH_YEAR.match(text):
            month = _MONTHS.get(found.group(2).lower())
            return date(_year(found.group(3)), month, int(found.group(1))) if month else None
        if found := _MONTH_DAY_YEAR.match(text):
            return date(_year(found.group(3)), int(found.group(1)), int(found.group(2)))
        if found := _ISO.match(text):
            return date(int(found.group(1)), int(found.group(2)), int(found.group(3)))
    except ValueError:
        return None
    return None


def _year(text: str) -> int:
    year = int(text)
    if len(text) == 4:
        return year
    return 2000 + year if year < 70 else 1900 + year


def _person(row: list[str], columns: dict[str, int]) -> ParsedContact | None:
    def cell(field: str) -> str:
        index = columns.get(field)
        return row[index] if index is not None and index < len(row) else ""

    name = _clean(cell("name"), LIMITS["name"]) or _clean(
        f"{cell('first')} {cell('last')}", LIMITS["name"]
    )
    if not name:
        return None
    return ParsedContact(
        name=name,
        company=_clean(cell("company"), LIMITS["company"]),
        position=_clean(cell("position"), LIMITS["position"]),
        email=_email(cell("email")),
        url=_url(cell("url")),
        school=_clean(cell("school"), LIMITS["school"]),
        connected_on=parse_date(cell("connected")),
    )


def parse_contacts(data: bytes) -> Parsed:
    """The people a file lists, in its order. Raises `Unreadable` for a file that is not a list
    of people."""
    if data.startswith(_PDF_MAGIC):
        raise Unreadable(415, NOT_A_LIST)
    if data.startswith(_ZIP_MAGIC):
        data = _from_archive(data)
    text = _decode(data)
    if not text.strip():
        raise Unreadable(400, EMPTY)
    layout = _layout(text)
    if layout is None:
        raise Unreadable(400, NO_NAMES)

    columns = layout.columns
    linkedin = {"first", "last", "connected"} <= set(columns)
    contacts: list[ParsedContact] = []
    skipped = 0
    try:
        for index, row in enumerate(_rows(text, layout.delimiter)):
            if index <= layout.header_at or not any(cell.strip() for cell in row):
                continue
            if index - layout.header_at > MAX_ROWS:
                raise Unreadable(413, TOO_MANY)
            person = _person(row, columns)
            if person is None:
                skipped += 1
            else:
                contacts.append(person)
    except csv.Error:
        raise Unreadable(415, NOT_A_LIST) from None
    if not contacts:
        raise Unreadable(400, EMPTY)
    return Parsed("linkedin" if linkedin else "csv", contacts, skipped)
