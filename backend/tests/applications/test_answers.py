"""Saved answers (#21): which question a form's label is asking, what a field is answered with
and where that came from, and what the four work-status answers rule out of a search."""

from __future__ import annotations

from typing import Any

import pytest

from apps.applications import answers
from apps.applications.answers import (
    DECLINE,
    SavedAnswers,
    choose_option,
    classify,
    load,
    plan,
    question_key,
    save,
    work_status,
)
from apps.applications.models import AnswerKind, ApplicationAnswer
from apps.matching.prefilter import DerivedCriteria, prefilter_jobs
from tests.accounts.helpers import Api
from tests.factories import make_job, make_user
from tests.pages.conftest import get_page, signed_in

K = AnswerKind


@pytest.mark.parametrize(
    ("label", "kind"),
    [
        # Greenhouse, Lever and Ashby forms, as they word it.
        ("Are you legally authorized to work in the United States?", K.WORK_AUTHORIZATION),
        ("Are you authorised to work in the country you are applying in?", K.WORK_AUTHORIZATION),
        ("Do you have the right to work in the UK?", K.WORK_AUTHORIZATION),
        (
            "Will you now or in the future require sponsorship for employment visa status?",
            K.SPONSORSHIP,
        ),
        # About sponsorship though it opens with authorization.
        ("Are you authorized to work in the US without sponsorship?", K.SPONSORSHIP),
        ("Do you require H-1B visa support?", K.SPONSORSHIP),
        ("Are you a U.S. citizen or lawful permanent resident?", K.CITIZENSHIP),
        ("Do you currently hold an active security clearance?", K.CLEARANCE),
        ("What are your salary expectations?", K.SALARY),
        ("Desired compensation", K.SALARY),
        ("How many years of professional software experience do you have?", K.YEARS_EXPERIENCE),
        ("When can you start?", K.START_DATE),
        ("What is your notice period?", K.START_DATE),
        ("Are you open to relocation?", K.RELOCATION),
        ("LinkedIn Profile", K.LINKEDIN),
        ("GitHub URL", K.GITHUB),
        ("Website / Portfolio", K.WEBSITE),
        ("Phone", K.PHONE),
        ("Where are you currently located?", K.LOCATION),
        ("How did you hear about this job?", K.HOW_HEARD),
        ("Have you previously been employed by Acme?", K.PREVIOUSLY_EMPLOYED),
        ("Are you at least 18 years of age?", K.OVER_18),
        ("Gender", K.GENDER),
        ("What is your race or ethnicity?", K.RACE),
        ("Are you Hispanic/Latino?", K.HISPANIC),
        ("Veteran Status", K.VETERAN),
        ("Disability Status", K.DISABILITY),
        ("What pronouns do you use?", K.PRONOUNS),
        # A question of the employer's own.
        ("Why do you want to work at Acme?", K.CUSTOM),
        ("Describe a project you are proud of.", K.CUSTOM),
        ("First name", K.CUSTOM),
    ],
)
def test_a_form_label_is_one_of_the_shared_questions_or_custom(
    label: str, kind: AnswerKind
) -> None:
    assert classify(label) == kind


def test_a_custom_question_is_itself_however_it_is_spaced_or_punctuated() -> None:
    key = question_key("Why do you want to work here?")
    assert key == question_key("  why do you want  to work here ")
    assert key != question_key("Why do you want to work there?")


@pytest.mark.django_db
def test_an_answer_is_saved_once_changed_in_place_and_forgotten_when_emptied() -> None:
    user = make_user()
    assert save(user.pk, K.SPONSORSHIP, " Yes ") is True
    assert save(user.pk, K.SPONSORSHIP, "no") is True
    assert save(user.pk, K.SALARY, "  $140,000 base  ") is True
    assert save(user.pk, K.CUSTOM, "I use it daily.", "Why do you want to work here?") is True
    saved = load(user.pk)
    assert saved.shared == {"sponsorship": "no", "salary": "$140,000 base"}
    assert saved.get(K.CUSTOM, "why do you want to work here") == "I use it daily."
    assert ApplicationAnswer.objects.for_user(user.pk).count() == 3

    assert save(user.pk, K.SALARY, "  ") is False
    assert load(user.pk).get(K.SALARY) is None
    # Someone else's answers are their own.
    assert load(make_user().pk) == SavedAnswers({}, {})


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("kind", "answer", "question"),
    [
        (K.SPONSORSHIP, "maybe", ""),  # a yes/no takes yes or no
        (K.CITIZENSHIP, "yes", ""),  # a choice takes one of its values
        (K.CUSTOM, "An answer", ""),  # a custom answer needs its question
    ],
)
def test_an_answer_the_question_cannot_take_is_refused(
    kind: AnswerKind, answer: str, question: str
) -> None:
    with pytest.raises(ValueError):
        save(make_user().pk, kind, answer, question)


# --- answering a field ---------------------------------------------------------------------------

SAVED = SavedAnswers(
    shared={
        "sponsorship": "no",
        "work_authorization": "yes",
        "citizenship": "permanent_resident",
        "salary": "$140,000",
        "veteran": "I am not a protected veteran",
    },
    custom={question_key("Why do you want to work here?"): "I use it daily."},
)


def test_a_field_is_answered_from_what_was_saved_or_not_at_all() -> None:
    sponsorship = plan(SAVED, "Will you now or in the future require sponsorship?", ["Yes", "No"])
    assert (sponsorship.value, sponsorship.source) == ("No", "saved")
    assert plan(SAVED, "Salary expectations").value == "$140,000"
    assert plan(SAVED, "Why do you want to work here?").value == "I use it daily."
    # A stored choice is written out the way a person would say it.
    assert plan(SAVED, "Are you a US citizen or permanent resident?").value == "Permanent resident"

    # Never made up: no saved answer, no answer.
    for label in ("How many years of experience do you have?", "Why this team?", "Start date"):
        missing = plan(SAVED, label)
        assert (missing.value, missing.source) == (None, "missing")


def test_a_voluntary_disclosure_nobody_answered_is_declined() -> None:
    options = ["Male", "Female", "Non-binary", "I don't wish to answer"]
    gender = plan(SAVED, "Gender", options)
    assert (gender.value, gender.source) == ("I don't wish to answer", "declined")
    assert plan(SAVED, "Disability Status").value == DECLINE
    # One the user did answer is theirs.
    veteran = plan(
        SAVED, "Veteran status", ["I am not a protected veteran", "I identify as a veteran"]
    )
    assert (veteran.value, veteran.source) == ("I am not a protected veteran", "saved")
    # A form with no way to decline gets nothing rather than a guess.
    assert plan(SAVED, "Gender", ["Male", "Female"]).value is None


@pytest.mark.parametrize(
    ("answer", "options", "chosen"),
    [
        ("yes", ["Yes", "No"], "Yes"),
        ("no", ["Yes, I will require sponsorship", "No, I will not"], "No, I will not"),
        ("citizen", ["US Citizen", "Permanent Resident", "Other"], "US Citizen"),
        ("permanent_resident", ["I am a U.S. citizen", "I am a permanent resident"],
         "I am a permanent resident"),
        (DECLINE, ["Yes", "No", "Prefer not to say"], "Prefer not to say"),
        ("yes", ["Yes, remote", "Yes, hybrid", "No"], None),  # two ways to say yes: ask
        ("$140,000", ["Under $100k", "$100k to $150k"], None),
    ],
)  # fmt: skip
def test_a_saved_answer_finds_its_option(
    answer: str, options: list[str], chosen: str | None
) -> None:
    assert choose_option(answer, options) == chosen


# --- who may be hired ----------------------------------------------------------------------------


def test_only_what_the_user_said_rules_a_posting_out() -> None:
    assert work_status(SavedAnswers({}, {})).excluded_work_auth == []
    assert work_status(SavedAnswers({"sponsorship": "no"}, {})).excluded_work_auth == []
    needs = work_status(SavedAnswers({"sponsorship": "yes"}, {}))
    assert needs.excluded_work_auth == ["citizen_only", "no_sponsorship", "us_person"]
    resident = work_status(SavedAnswers({"citizenship": "permanent_resident"}, {}))
    assert resident.excluded_work_auth == ["citizen_only"]
    neither = work_status(SavedAnswers({"citizenship": "neither"}, {}))
    assert neither.excluded_work_auth == ["citizen_only", "us_person"]
    assert work_status(SavedAnswers({"clearance": "none"}, {})).lacks_clearance is True
    assert work_status(SavedAnswers({"clearance": "secret"}, {})).lacks_clearance is False
    assert work_status(SavedAnswers({}, {})).lacks_clearance is False


@pytest.mark.django_db
def test_a_search_leaves_out_postings_the_users_own_answers_rule_out() -> None:
    user = make_user(with_resume=True)
    open_to_all = make_job(title="Software Engineer")
    citizens = make_job(title="Software Engineer, Guidance", work_auth="citizen_only")
    no_visa = make_job(title="Software Engineer, Payments", work_auth="no_sponsorship")
    cleared = make_job(title="Software Engineer, Systems", clearance="required")
    criteria = DerivedCriteria(title_include=["software engineer"], title_exclude=[], keywords=[])

    def found() -> set[str]:
        from apps.accounts.schemas import Preferences

        return set(prefilter_jobs(user.pk, Preferences(), criteria).job_ids)

    everything = {open_to_all.id, citizens.id, no_visa.id, cleared.id}
    assert found() == everything  # nothing saved, nothing ruled out

    save(user.pk, K.SPONSORSHIP, "yes")
    assert found() == {open_to_all.id, cleared.id}
    save(user.pk, K.SPONSORSHIP, "no")
    save(user.pk, K.CLEARANCE, "none")
    assert found() == {open_to_all.id, citizens.id, no_visa.id}


# --- the endpoint and the page -------------------------------------------------------------------


@pytest.mark.django_db
def test_the_profile_page_lists_every_shared_question_with_its_answer() -> None:
    user = make_user()
    client = signed_in(user)
    api = Api(client)
    response = api.put(
        "/api/answers",
        {
            "answers": [
                {"kind": "sponsorship", "answer": "no"},
                {"kind": "salary", "answer": "$140,000"},
                {"kind": "custom", "answer": "Daily.", "question": "Why do you want to work here?"},
            ]
        },
    )
    assert response.status_code == 200 and response.json() == {
        "ok": True,
        "message": "Answers saved",
    }

    listed: list[dict[str, Any]] = get_page(client, "/api/pages/profile").json()["answers"]
    assert [item["kind"] for item in listed[: len(answers.CATALOGUE)]] == [
        question.kind for question in answers.CATALOGUE
    ]
    by_kind = {item["kind"]: item for item in listed}
    assert by_kind["sponsorship"]["answer"] == "no"
    assert by_kind["sponsorship"]["options"] == [
        {"value": "yes", "label": "Yes"},
        {"value": "no", "label": "No"},
    ]
    assert by_kind["citizenship"]["answer"] is None and by_kind["citizenship"]["group"] == "work"
    assert listed[-1] == {
        "kind": "custom",
        "question": "Why do you want to work here?",
        "input": "text",
        "options": [],
        "group": "custom",
        "hint": "",
        "answer": "Daily.",
    }

    # An answer the question cannot take is refused in a sentence, and nothing is saved.
    refused = api.put("/api/answers", {"answers": [{"kind": "sponsorship", "answer": "perhaps"}]})
    assert refused.status_code == 400
    assert refused.json() == {"ok": False, "message": "Those answers could not be read."}
    assert load(user.pk).get(K.SPONSORSHIP) == "no"
