"""`/api/contacts` and the Contacts page (#63): the import over HTTP, what the page shows per
company, the board lookup it offers, search, and removal."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from apps.accounts.models import User
from apps.contacts import page as contacts_page
from apps.contacts import parse
from apps.contacts.models import Contact
from apps.core import throttle
from apps.runs.models import Run
from tests.factories import make_company, make_contact, make_job, make_user
from tests.pages.conftest import ORIGIN, SSR, signed_in
from tests.queue import deferred

pytestmark = pytest.mark.django_db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "contacts"
LINKEDIN = (FIXTURES / "linkedin-connections.csv").read_bytes()
ALUMNI = (FIXTURES / "alumni.csv").read_bytes()


class Browser:
    """A signed-in browser: multipart uploads, JSON bodies and the Origin header."""

    def __init__(self, user: User | None = None) -> None:
        self.user = user or make_user()
        self.client: Client = signed_in(self.user)

    def upload(self, data: bytes, name: str = "Connections.csv") -> Any:
        return self.client.post(
            "/api/contacts/import",
            {"file": SimpleUploadedFile(name, data, content_type="text/csv")},
            headers={"Origin": ORIGIN},
        )

    def post(self, path: str) -> Any:
        return self.client.post(
            path, data="{}", content_type="application/json", headers={"Origin": ORIGIN}
        )

    def delete(self, path: str) -> Any:
        return self.client.delete(path, headers={"Origin": ORIGIN})

    def page(self, query: str = "") -> dict[str, Any]:
        response = self.client.get(f"/api/pages/contacts{query}", headers=SSR)
        assert response.status_code == 200, response.content
        return response.json()


def names(items: list[dict[str, Any]]) -> list[str]:
    return [item["name"] for item in items]


# --- the import, and what the page then shows ----------------------------------------------------


def test_an_import_shows_how_many_people_the_user_knows_at_each_company_in_the_mirror() -> None:
    """#63's "Done when": per company in the mirror, how many people; and the same file again
    changes nothing."""
    browser = Browser()
    stripe = make_company(name="Stripe", slug="stripe")
    make_company(name="Linear", slug="linear", source_type="ashby")
    make_job(company=stripe)
    make_job(company=stripe)
    make_job(company=stripe, is_active=False)

    response = browser.upload(LINKEDIN)
    assert response.status_code == 200, response.content
    assert response.json() == {
        "ok": True,
        "message": "Added 8 people. 1 row with no name was left out.",
        "added": 8,
        "updated": 0,
        "unchanged": 0,
        "skipped": 1,
        "total": 8,
        "matched": 3,
    }

    page = browser.page()
    assert (page["total"], page["matched"], page["withoutCompany"]) == (8, 3, 1)
    assert page["companiesTotal"] == 2
    assert page["companies"] == [
        {
            "slug": "stripe",
            "name": "Stripe",
            "peopleCount": 2,
            "openPostings": 2,
            "people": [
                {"id": page["companies"][0]["people"][0]["id"], "name": "Priya Rao"}
                | {"position": "Staff Engineer"},
                {"id": page["companies"][0]["people"][1]["id"], "name": "Tomasz Zielinski"}
                | {"position": "Engineering Manager"},
            ],
        },
        {
            "slug": "linear",
            "name": "Linear",
            "peopleCount": 1,
            "openPostings": 0,
            "people": [
                {
                    "id": page["companies"][1]["people"][0]["id"],
                    "name": "Aiko Tanaka",
                    "position": "Product Designer",
                }
            ],
        },
    ]
    # Companies the mirror does not have, most people first; two spellings are one company.
    assert [(row["name"], row["peopleCount"]) for row in page["unmatched"]] == [
        ("Globex Analytics", 2),
        ("Northwind Robotics, Inc.", 1),
        ("Self-employed", 1),
    ]
    assert names(page["unmatched"][0]["people"]) == ["Lena Fischer", "Omar Haddad"]
    assert page["unmatchedTotal"] == 3
    assert page["results"] is None

    stored = sorted(Contact.objects.for_user(browser.user.pk).values_list("id", "updated_at"))
    again = browser.upload(LINKEDIN)
    assert again.status_code == 200
    assert (again.json()["added"], again.json()["updated"], again.json()["unchanged"]) == (0, 0, 8)
    assert sorted(Contact.objects.for_user(browser.user.pk).values_list("id", "updated_at")) == (
        stored
    )
    assert browser.page() == page
    assert deferred() == []  # an import queues nothing


def test_the_archive_linkedin_sends_is_imported_as_it_is() -> None:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Connections.csv", LINKEDIN)
    response = Browser().upload(buffer.getvalue(), "Basic_LinkedInDataExport.zip")
    assert response.status_code == 200
    assert response.json()["added"] == 8


def test_uploads_that_are_not_contact_lists_are_refused_and_store_nothing() -> None:
    browser = Browser()
    cases = [
        (b"%PDF-1.7 a resume", 415, parse.NOT_A_LIST),
        (b"Company,Position\nStripe,Engineer\n", 400, parse.NO_NAMES),
        (b"First Name,Last Name\n", 400, parse.EMPTY),
        (b"x" * (10 * 1024 * 1024 + 1), 413, "That file is 10.0 MB; the limit is 10 MB."),
    ]
    for data, status, message in cases:
        response = browser.upload(data)
        assert response.status_code == status, response.content
        assert response.json() == {"ok": False, "message": message}
    missing = browser.client.post("/api/contacts/import", {}, headers={"Origin": ORIGIN})
    assert missing.status_code == 400
    assert missing.json() == {"ok": False, "message": "No file uploaded."}
    assert not Contact.objects.for_user(browser.user.pk).exists()


def test_imports_are_throttled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(throttle, "CONTACTS_IMPORT", throttle.Limit(2, 3600))
    browser = Browser()
    assert browser.upload(ALUMNI).status_code == 200
    assert browser.upload(ALUMNI).status_code == 200
    refused = browser.upload(ALUMNI)
    assert refused.status_code == 429
    assert refused.json() == {"ok": False, "message": throttle.TOO_MANY}


def test_contacts_need_a_session() -> None:
    anonymous = Client()
    upload = anonymous.post(
        "/api/contacts/import",
        {"file": SimpleUploadedFile("c.csv", ALUMNI)},
        headers={"Origin": ORIGIN},
    )
    assert upload.status_code == 401
    assert anonymous.delete("/api/contacts", headers={"Origin": ORIGIN}).status_code == 401
    assert anonymous.post("/api/contacts/lookup", headers={"Origin": ORIGIN}).status_code == 401


# --- the page ------------------------------------------------------------------------------------


def test_the_page_of_a_user_with_no_contacts_costs_one_read(
    django_assert_num_queries: Any,
) -> None:
    browser = Browser()
    with django_assert_num_queries(1 + 2):  # the page's one read, after the session and user
        page = browser.page()
    assert page == {
        "total": 0,
        "matched": 0,
        "withoutCompany": 0,
        "companies": [],
        "companiesTotal": 0,
        "unmatched": [],
        "unmatchedTotal": 0,
        "lookup": {"names": [], "remaining": 0, "runId": None},
        "query": "",
        "results": None,
        "resultsTotal": 0,
    }


def test_a_company_registered_a_moment_ago_shows_without_waiting_for_a_rematch() -> None:
    browser = Browser()
    browser.upload(LINKEDIN)
    assert browser.page()["companies"] == []

    make_company(name="Globex Analytics", slug="globex-analytics")
    page = browser.page()
    assert [(row["name"], row["peopleCount"]) for row in page["companies"]] == [
        ("Globex Analytics", 2)
    ]
    assert "Globex Analytics" not in names(page["unmatched"])
    # Nothing was written by reading the page.
    assert not Contact.objects.for_user(browser.user.pk).filter(company__isnull=False).exists()


def test_search_finds_people_by_name_company_position_or_school() -> None:
    browser = Browser()
    make_company(name="Linear", slug="linear", source_type="ashby")
    browser.upload(LINKEDIN)
    browser.upload(ALUMNI)

    page = browser.page("?q=linear")
    assert page["query"] == "linear"
    assert names(page["results"]) == ["Aiko Tanaka", "Noor Haddad"]
    assert page["resultsTotal"] == 2
    noor = page["results"][1]
    assert noor == {
        "id": noor["id"],
        "name": "Noor Haddad",
        "position": "Software Engineer",
        "companyName": "Linear",
        "companySlug": "linear",
        "email": "noor.haddad@gmail.com",
        "profileUrl": "https://linkedin.com/in/noor-haddad-example",
        "connectedOn": None,
    }
    assert names(browser.page("?q=ENGINEER%20stripe")["results"]) == [
        "Priya Rao",
        "Tomasz Zielinski",
    ]
    assert names(browser.page("?q=westfield")["results"]) == [
        "Caleb Mwangi",
        "Noor Haddad",
        "Sofia Lindqvist",
    ]
    unmatched = browser.page("?q=lena")["results"][0]
    assert (unmatched["companyName"], unmatched["companySlug"]) == ("Globex Analytics", None)
    # A company is found however a file spelled it, and punctuation is not part of a word.
    assert names(browser.page("?q=Stripe%2C%20Inc.")["results"]) == [
        "Priya Rao",
        "Tomasz Zielinski",
    ]
    assert names(browser.page("?q=globex-analytics")["results"]) == ["Lena Fischer", "Omar Haddad"]
    assert names(browser.page("?q=recruiter%2C%20engineering")["results"]) == ["Marcus Bell"]
    assert browser.page("?q=nobody-by-this-name")["results"] == []
    assert browser.page("?q=%21%3F")["results"] == []
    assert browser.page("?q=%20%20")["results"] is None


def test_lists_are_capped_and_their_counts_are_not(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(contacts_page, "PEOPLE_SHOWN", 2)
    monkeypatch.setattr(contacts_page, "COMPANIES_SHOWN", 1)
    monkeypatch.setattr(contacts_page, "UNMATCHED_SHOWN", 1)
    monkeypatch.setattr(contacts_page, "UNMATCHED_PEOPLE", 1)
    monkeypatch.setattr(contacts_page, "RESULTS_SHOWN", 2)
    browser = Browser()
    stripe = make_company(name="Stripe", slug="stripe")
    linear = make_company(name="Linear", slug="linear", source_type="ashby")
    for n in range(4):
        make_contact(browser.user, name=f"Person {n}", company=stripe)
    make_contact(browser.user, name="Person 4", company=linear)
    for n in range(5, 8):
        make_contact(browser.user, name=f"Person {n}", company_name="Initech")
    make_contact(browser.user, name="Person 8", company_name="Hooli")

    page = browser.page("?q=person")
    assert (page["total"], page["matched"]) == (9, 5)
    assert [(row["name"], row["peopleCount"], len(row["people"])) for row in page["companies"]] == [
        ("Stripe", 4, 2)
    ]
    assert page["companiesTotal"] == 2
    assert [(row["name"], row["peopleCount"], len(row["people"])) for row in page["unmatched"]] == [
        ("Initech", 3, 1)
    ]
    assert page["unmatchedTotal"] == 2
    assert (len(page["results"]), page["resultsTotal"]) == (2, 9)


# --- looking for the boards of companies the user knows people at --------------------------------


def lookup_setup(browser: Browser, companies: int) -> None:
    """`companies` unmatched companies, the n-th named by n+1 people, and some non-companies."""
    for n in range(companies):
        for person in range(n + 1):
            make_contact(browser.user, name=f"P{n}-{person}", company_name=f"Unlisted {n:02d}")
    for person in range(40):
        make_contact(browser.user, name=f"Solo {person}", company_name="Self-Employed")


def test_a_lookup_takes_the_companies_most_contacts_work_at_and_only_when_asked() -> None:
    browser = Browser()
    lookup_setup(browser, 25)
    assert not Run.objects.for_user(browser.user.pk).exists()  # nothing starts unasked

    page = browser.page()
    wanted = [f"Unlisted {n:02d}" for n in range(24, 4, -1)]  # the 20 with the most people
    assert page["lookup"] == {"names": wanted, "remaining": 25, "runId": None}
    assert page["unmatched"][0]["name"] == "Self-Employed"  # listed, but never looked up

    response = browser.post("/api/contacts/lookup")
    assert response.status_code == 202, response.content
    body = response.json()
    assert body == {
        "ok": True,
        "runId": body["runId"],
        "message": "Looking for the job boards of 20 companies your contacts work at.",
        "names": wanted,
    }
    run = Run.objects.for_user(browser.user.pk).get(id=body["runId"])
    assert (run.kind, run.status) == ("company_discovery", "queued")
    assert run.input["names"] == wanted
    [job] = deferred("company.add")
    assert job.args == {"run_id": str(run.id)}
    # People's names are no part of what a lookup is given.
    assert "P24-0" not in json.dumps(run.input)

    # While it runs, the page points at it and offers the rest.
    page = browser.page()
    assert page["lookup"] == {
        "names": [f"Unlisted {n:02d}" for n in range(4, -1, -1)],
        "remaining": 5,
        "runId": str(run.id),
    }
    assert not any(row["lookedUp"] for row in page["unmatched"])

    # Finished without registering anything: those names are marked, and not tried again.
    Run.objects.for_user(browser.user.pk).filter(id=run.id).update(status="completed")
    page = browser.page()
    assert page["lookup"]["runId"] is None
    assert {row["name"] for row in page["unmatched"] if row["lookedUp"]} == set(wanted)

    second = browser.post("/api/contacts/lookup").json()
    assert second["names"] == [f"Unlisted {n:02d}" for n in range(4, -1, -1)]
    Run.objects.for_user(browser.user.pk).filter(id=second["runId"]).update(status="completed")

    nothing = browser.post("/api/contacts/lookup")
    assert nothing.status_code == 400
    assert nothing.json() == {"ok": False, "message": contacts_page.NOTHING_TO_LOOK_UP}
    assert Run.objects.for_user(browser.user.pk).count() == 2


def test_a_failed_or_stopped_lookup_tried_nothing() -> None:
    browser = Browser()
    lookup_setup(browser, 2)
    first = browser.post("/api/contacts/lookup").json()
    Run.objects.for_user(browser.user.pk).filter(id=first["runId"]).update(status="failed")
    page = browser.page()
    assert page["lookup"]["names"] == ["Unlisted 01", "Unlisted 00"]
    assert not any(row["lookedUp"] for row in page["unmatched"])


def test_one_users_lookups_say_nothing_about_anothers() -> None:
    a, b = Browser(), Browser()
    lookup_setup(a, 2)
    lookup_setup(b, 2)
    a.post("/api/contacts/lookup")
    assert b.page()["lookup"] == {
        "names": ["Unlisted 01", "Unlisted 00"],
        "remaining": 2,
        "runId": None,
    }


# --- removing ------------------------------------------------------------------------------------


def test_removing_one_contact_and_all_of_them() -> None:
    a, b = Browser(), Browser()
    a.upload(LINKEDIN)
    b.upload(ALUMNI)
    priya = Contact.objects.for_user(a.user.pk).get(name="Priya Rao")

    theirs = b.delete(f"/api/contacts/{priya.id}")
    assert theirs.status_code == 404
    assert theirs.json() == {"ok": False, "message": "That contact was not found."}
    assert b.delete("/api/contacts/not-a-uuid").status_code == 404

    mine = a.delete(f"/api/contacts/{priya.id}")
    assert mine.status_code == 200
    assert mine.json() == {"ok": True, "message": "Contact removed."}
    assert a.delete(f"/api/contacts/{priya.id}").status_code == 404
    assert Contact.objects.for_user(a.user.pk).count() == 7

    everything = a.delete("/api/contacts")
    assert everything.json() == {"ok": True, "message": "Removed 7 contacts."}
    assert a.delete("/api/contacts").json() == {
        "ok": True,
        "message": "There were no contacts to remove.",
    }
    assert Contact.objects.for_user(b.user.pk).count() == 3  # B's are B's
