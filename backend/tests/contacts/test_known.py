"""Who the user knows at the company behind a posting (#64): the lookup, and where a match's
row and page say it."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from apps.contacts.known import (
    PAGE_SHOWN,
    ROW_SHOWN,
    count_matches_with_contacts,
    known_at,
    known_at_jobs,
)
from apps.matching import queries
from tests.factories import make_company, make_contact, make_job, make_match, make_user
from tests.pages.conftest import SSR, signed_in

pytestmark = pytest.mark.django_db


def names(people: list[Any]) -> list[str]:
    return [person.name for person in people]


# --- the lookup ----------------------------------------------------------------------------------


def test_a_posting_is_where_the_user_knows_someone_by_company_or_by_name() -> None:
    user = make_user()
    acme = make_company(name="Acme", slug="acme")
    globex = make_company(name="Globex", slug="globex")
    at_acme = make_job(company=acme)
    at_globex = make_job(company=globex)
    # An aggregator's posting: no registered company, only the name it was listed under.
    listed = make_job(company=acme, company_name="Initech, Inc.", source="jsearch")
    listed.company = None
    listed.save(update_fields=["company"])

    make_contact(user, name="Linked By Company", company=acme, company_name="ACME Corp")
    # Imported before Acme was registered: only the name ties them to it.
    make_contact(user, name="Linked By Name", company_name="Acme")
    make_contact(user, name="At Initech", company_name="Initech")
    make_contact(user, name="Elsewhere", company_name="Hooli")

    found = known_at_jobs(user.pk, [at_acme, at_globex, listed])
    assert set(found) == {at_acme.id, listed.id}  # nobody at Globex: left out
    assert sorted(names(found[at_acme.id])) == ["Linked By Company", "Linked By Name"]
    assert names(found[listed.id]) == ["At Initech"]


def test_people_come_most_recently_connected_first_and_each_once() -> None:
    user = make_user()
    acme = make_company(name="Acme", slug="acme")
    job = make_job(company=acme)
    make_contact(user, name="Old Friend", company=acme, connected_on=date(2015, 6, 1))
    make_contact(user, name="New Colleague", company=acme, connected_on=date(2026, 9, 1))
    make_contact(user, name="Zed Undated", company=acme)
    make_contact(user, name="Abe Undated", company=acme)

    people = known_at_jobs(user.pk, [job])[job.id]
    # Linked by company and by name both: still one row each.
    assert names(people) == ["New Colleague", "Old Friend", "Abe Undated", "Zed Undated"]
    assert people[0].position == "" and people[0].profile_url == ""


def test_one_users_contacts_say_nothing_about_anothers_postings() -> None:
    user, other = make_user(), make_user()
    acme = make_company(name="Acme", slug="acme")
    job = make_job(company=acme)
    make_contact(other, name="Theirs", company=acme)
    assert known_at_jobs(user.pk, [job]) == {}
    assert names(known_at_jobs(other.pk, [job])[job.id]) == ["Theirs"]


def test_any_number_of_postings_cost_one_query(django_assert_num_queries: Any) -> None:
    user = make_user()
    companies = [make_company(name=f"Company {n}") for n in range(6)]
    jobs = [make_job(company=company) for company in companies for _ in range(3)]
    for company in companies[:4]:
        make_contact(user, company=company)
    with django_assert_num_queries(1):
        found = known_at_jobs(user.pk, jobs)
    assert len(found) == 12
    with django_assert_num_queries(0):
        assert known_at(user.pk, []) == {}
        assert known_at(user.pk, [("web:x:1", None, "株式会社")]) == {}


def test_counting_the_matches_where_the_user_knows_someone() -> None:
    user, other = make_user(), make_user()
    acme = make_company(name="Acme", slug="acme")
    make_contact(user, company=acme)
    here = make_match(user, job=make_job(company=acme))
    there = make_match(user, job=make_job())
    theirs = make_match(other, job=make_job(company=acme))
    assert count_matches_with_contacts(user.pk, [str(here.id), str(there.id)]) == 1
    assert count_matches_with_contacts(user.pk, [str(there.id)]) == 0
    assert count_matches_with_contacts(user.pk, [str(theirs.id)]) == 0  # not this user's match
    assert count_matches_with_contacts(user.pk, []) == 0


# --- a match's row -------------------------------------------------------------------------------


def test_rows_say_who_the_user_knows_only_when_asked_and_in_one_query(
    django_assert_num_queries: Any,
) -> None:
    user = make_user()
    acme = make_company(name="Acme", slug="acme")
    for n in range(ROW_SHOWN + 2):
        make_contact(
            user,
            name=f"Person {n}",
            company=acme,
            position=f"Engineer {n}",
            connected_on=date(2020 + n, 1, 1),
        )
    known = make_match(user, job=make_job(company=acme), fast_score=90)
    unknown = make_match(user, job=make_job(), fast_score=80)

    rows = queries.list_matches(user.pk)
    assert [row.knows for row in rows] == [None, None]  # a pipeline's rows do not ask
    with django_assert_num_queries(1):
        shown = queries.with_known(user.pk, rows)
    assert [row.match_id for row in shown] == [known.id, unknown.id]
    assert shown[1].knows is None
    knows = shown[0].knows
    assert knows is not None
    assert knows.count == ROW_SHOWN + 2
    assert [(person.name, person.position) for person in knows.people] == [
        ("Person 4", "Engineer 4"),
        ("Person 3", "Engineer 3"),
        ("Person 2", "Engineer 2"),
    ]
    with django_assert_num_queries(0):
        assert queries.with_known(user.pk, []) == []
    # On the wire the row carries `knows`, and nothing of how it was looked up.
    dumped = shown[0].model_dump(by_alias=True)
    assert set(dumped["knows"]) == {"count", "people"}
    assert "companyId" not in dumped and "_company_id" not in dumped


def test_the_pages_and_cards_that_show_rows_say_who_the_user_knows() -> None:
    user = make_user(with_resume=True)
    acme = make_company(name="Acme", slug="acme")
    make_contact(user, name="Priya Rao", company=acme, position="Staff Engineer")
    known = make_match(user, job=make_job(company=acme), fast_score=90, deep_score=88)
    unknown = make_match(user, job=make_job(), fast_score=80, deep_score=75)
    client = signed_in(user)
    expected = {
        "count": 1,
        "people": [
            {
                "id": client.get("/api/pages/contacts", headers=SSR).json()["companies"][0][
                    "people"
                ][0]["id"],
                "name": "Priya Rao",
                "position": "Staff Engineer",
                "profileUrl": "",
            }
        ],
    }

    def by_id(rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {row["matchId"]: row["knows"] for row in rows}

    wanted = {str(known.id): expected, str(unknown.id): None}
    matches = client.get("/api/pages/matches", headers=SSR).json()["matches"]
    assert by_id(matches) == wanted
    home = client.get("/api/pages/home", headers=SSR).json()
    assert by_id(home["best"]) == wanted
    assert by_id(home["newThisWeek"]) == wanted
    cards = client.get(f"/api/matches?ids={known.id},{unknown.id}", headers=SSR).json()
    assert by_id(cards["matches"]) == wanted


# --- a match's page ------------------------------------------------------------------------------


def test_the_match_page_lists_the_people_the_user_knows_there() -> None:
    user = make_user(with_resume=True)
    acme = make_company(name="Acme", slug="acme")
    for n in range(PAGE_SHOWN + 3):
        make_contact(
            user,
            name=f"Person {n:02d}",
            company=acme,
            position="Engineer",
            profile_url=f"https://www.linkedin.com/in/person-{n:02d}-example",
        )
    known = make_match(user, job=make_job(company=acme))
    unknown = make_match(user, job=make_job())
    client = signed_in(user)

    page = client.get(f"/api/pages/matches/{known.id}", headers=SSR).json()
    assert page["knows"]["count"] == PAGE_SHOWN + 3
    assert [person["name"] for person in page["knows"]["people"]] == [
        f"Person {n:02d}" for n in range(PAGE_SHOWN)
    ]
    assert page["knows"]["people"][0] == {
        "id": page["knows"]["people"][0]["id"],
        "name": "Person 00",
        "position": "Engineer",
        "profileUrl": "https://www.linkedin.com/in/person-00-example",
        "note": None,  # nothing drafted to them yet (#25)
    }
    # A posting where they know nobody: the page says so with an empty list, as before.
    nobody = client.get(f"/api/pages/matches/{unknown.id}", headers=SSR).json()
    assert nobody["knows"] == {"count": 0, "people": []}
