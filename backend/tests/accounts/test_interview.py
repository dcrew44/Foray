"""The interview, onboarding, `PUT /api/preferences` and `PATCH /api/settings` (design §5.3, §12.2:
`interview.test.ts`'s 10 cases plus the profile page's `slots`)."""

from __future__ import annotations

from typing import Any

import pytest
from django.test import Client

from apps.accounts import profile
from apps.accounts.models import User
from apps.accounts.profile import (
    SLOTS,
    interview_progress,
    is_filled,
    onboarding_state,
)
from apps.accounts.schemas import InterviewState, Preferences, Settings
from tests.accounts.helpers import load_golden
from tests.factories import make_resume, make_user

EMPTY = Preferences()
FRESH = InterviewState()


def prefs(**fields: Any) -> Preferences:
    return Preferences.model_validate(fields)


def ids(slots: list[profile.Slot]) -> list[str]:
    return [slot.id for slot in slots]


# --- Preferences (interview.test.ts: PreferencesSchema) ------------------------------------------


def test_fills_in_keys_a_stored_row_does_not_have() -> None:
    # What an older version of this schema would have written. Missing keys must not fail the
    # read, because the fallback would be empty preferences, discarding the answers.
    stored = Preferences.read({"target_roles": ["Backend Engineer"], "seniority": "senior"})
    assert stored.target_roles == ["Backend Engineer"]
    assert stored.work_auth is None
    assert stored.salary_floor is None


def test_parses_a_completely_empty_row() -> None:
    assert Preferences.model_validate({}) == EMPTY
    assert EMPTY.dump() == dict.fromkeys(
        [
            "target_roles",
            "seniority",
            "locations",
            "remote_pref",
            "salary_floor",
            "work_auth",
            "start_date",
            "must_haves",
            "dealbreakers",
        ]
    )


def test_still_rejects_a_value_of_the_wrong_type() -> None:
    # Tolerance is about absent keys, not about accepting nonsense.
    with pytest.raises(ValueError):
        Preferences.model_validate({"seniority": "emperor"})
    with pytest.raises(ValueError):
        Preferences.model_validate({"target_roles": "engineer"})


# --- interview_progress ---------------------------------------------------------------------------


def test_counts_nothing_as_answered_for_a_fresh_user() -> None:
    progress = interview_progress(EMPTY, FRESH)
    assert progress.answered == 0
    assert progress.total == len(SLOTS) == 8
    assert progress.complete is False
    assert len(progress.blocking) > 0


def test_treats_a_declined_slot_as_answered() -> None:
    progress = interview_progress(EMPTY, InterviewState(version=1, skipped=["salary_floor"]))
    assert progress.answered == 1
    assert "salary_floor" not in ids(progress.outstanding)


def test_does_not_let_a_declined_slot_unblock_a_required_one() -> None:
    # Declining is allowed; declining your way into a search is not.
    progress = interview_progress(EMPTY, InterviewState(version=1, skipped=["work_auth"]))
    assert "work_auth" not in ids(progress.outstanding)
    assert "work_auth" in ids(progress.blocking)


def test_treats_an_empty_array_as_unanswered() -> None:
    progress = interview_progress(prefs(target_roles=[]), FRESH)
    assert "target_roles" in ids(progress.blocking)


def test_counts_the_constraints_slot_when_either_field_is_given() -> None:
    progress = interview_progress(prefs(dealbreakers=["no on-call"]), FRESH)
    assert "constraints" in ids(progress.filled)
    constraints = profile.SLOT_BY_ID["constraints"]
    assert is_filled(constraints, prefs(must_haves=["ownership"], dealbreakers=[]))
    assert not is_filled(constraints, prefs(must_haves=[], dealbreakers=None))


def test_is_complete_once_every_slot_is_answered_or_declined() -> None:
    answered = prefs(
        target_roles=["Backend Engineer"],
        seniority="senior",
        locations=["Bay Area"],
        remote_pref="any",
        work_auth="US citizen",
        start_date="six weeks",
        must_haves=["ownership"],
    )
    progress = interview_progress(answered, InterviewState(version=1, skipped=["salary_floor"]))
    assert progress.complete is True
    assert progress.blocking == []


def test_defaults_an_empty_stored_state_to_the_current_version() -> None:
    assert InterviewState.read({}) == InterviewState(version=1, skipped=[])
    assert InterviewState.read({}).dump() == {"version": 1, "skipped": []}


# --- SLOTS and describe_preferences: byte for byte with the TypeScript --------------------------


def test_slots_match_the_typescript_prompt_golden() -> None:
    """The chat prompt's interview lists every slot as `- <label>[ (needed to search)]: <intent>`;
    the golden was captured from the TypeScript `SLOTS`."""
    instructions = load_golden("prompts", "chat_system_prompt", "interview_empty.json")[
        "instructions"
    ]
    block = instructions.split("Still to find out:\n", 1)[1].split("\n\n", 1)[0]
    expected = [
        f"- {slot.label}{' (needed to search)' if slot.required else ''}: {slot.intent}"
        for slot in SLOTS
    ]
    assert block.splitlines() == expected
    assert [slot.id for slot in SLOTS] == [
        "target_roles",
        "seniority",
        "locations",
        "remote_pref",
        "work_auth",
        "salary_floor",
        "start_date",
        "constraints",
    ]
    assert [slot.id for slot in SLOTS if slot.required] == [
        "target_roles",
        "seniority",
        "locations",
        "remote_pref",
        "work_auth",
    ]
    assert profile.SLOT_BY_ID["constraints"].fields == ["must_haves", "dealbreakers"]
    # Every preference field belongs to exactly one slot.
    owned = [field for slot in SLOTS for field in slot.fields]
    assert sorted(owned) == sorted(Preferences.model_fields)


@pytest.mark.parametrize(
    "case", load_golden("strings", "describe_preferences.json"), ids=lambda c: c["output"][:30]
)
def test_describe_preferences_golden(case: dict[str, Any]) -> None:
    assert profile.describe_preferences(Preferences.read(case["input"])) == case["output"]


def test_describe_preferences_prints_integral_floats_as_javascript_does() -> None:
    floor = {"amount": 150000.0, "currency": "USD", "period": "year"}
    text = profile.describe_preferences(prefs(salary_floor=floor))
    assert text == '- salary_floor: {"amount":150000,"currency":"USD","period":"year"}'


def test_slot_wire_shape() -> None:
    assert SLOTS[0].model_dump(by_alias=True) == {
        "id": "target_roles",
        "label": "Target roles",
        "intent": SLOTS[0].intent,
        "fields": ["target_roles"],
        "required": True,
    }


@pytest.mark.django_db
def test_profile_page_serves_slots() -> None:
    """`/api/pages/profile` serves `slots` equal to `SLOTS`, so the frontend keeps no copy."""
    user = make_user()
    client = Client()
    client.force_login(user)
    response = client.get("/api/pages/profile")
    if response.status_code == 404:
        pytest.skip("GET /api/pages/profile lands with the pages step")
    assert response.status_code == 200
    assert response.json()["slots"] == [slot.model_dump(by_alias=True) for slot in SLOTS]


# --- onboarding ----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_onboarding_without_a_resume(django_assert_max_num_queries: Any) -> None:
    user = make_user(preferences={"target_roles": ["Engineer"], "seniority": "mid"})
    with django_assert_max_num_queries(1):
        state = onboarding_state(user)
    assert state.has_resume is False
    assert state.resume is None
    assert state.ready is False
    assert state.preferences.target_roles == ["Engineer"]
    assert ids(state.progress.blocking) == ["locations", "remote_pref", "work_auth"]
    assert state.settings == Settings()

    wire = state.model_dump(mode="json", by_alias=True)
    assert set(wire) == {"hasResume", "resume", "preferences", "progress", "settings", "ready"}
    assert wire["preferences"]["target_roles"] == ["Engineer"]  # snake_case, as stored
    assert set(wire["settings"]) == {"defaultMode", "scoreThreshold", "deepEvalTopN"}
    assert set(wire["progress"]) == {
        "filled",
        "outstanding",
        "blocking",
        "answered",
        "total",
        "complete",
    }


@pytest.mark.django_db
def test_onboarding_with_a_resume_and_complete_preferences() -> None:
    user = make_user(with_resume=True)
    state = onboarding_state(user)
    assert state.has_resume is True
    assert state.resume is not None and state.resume.work
    assert state.progress.blocking == []
    assert state.ready is True

    # A resume with no work, education or skills does not count.
    empty = make_user(with_resume=False, preferences=user.preferences)
    make_resume(empty, data={"basics": {"name": "Nobody"}})
    assert onboarding_state(empty).has_resume is False
    assert onboarding_state(empty).ready is False


@pytest.mark.django_db
def test_onboarding_reads_unreadable_columns_as_defaults() -> None:
    user = make_user(
        preferences={"seniority": "emperor"},
        interview_state={"skipped": "nope"},
        settings={"scoreThreshold": 500},
    )
    state = onboarding_state(user)
    assert state.preferences == EMPTY
    assert state.progress.answered == 0
    assert state.settings == Settings()


# --- PUT /api/preferences ------------------------------------------------------------------------


@pytest.mark.django_db
def test_put_preferences_succeeds_without_a_resume(signed_in: Any) -> None:
    client, user = signed_in()
    assert onboarding_state(user).has_resume is False
    response = client.put(
        "/api/preferences",
        {
            "target_roles": ["Backend Engineer"],
            "seniority": "senior",
            "locations": ["Bay Area"],
            "remote_pref": "hybrid",
            "work_auth": "US citizen",
        },
    )
    assert response.status_code == 200, response.content
    assert response.json() == {"ok": True, "message": "Preferences saved"}
    user.refresh_from_db()
    assert user.preferences["target_roles"] == ["Backend Engineer"]
    assert onboarding_state(user).progress.blocking == []


@pytest.mark.django_db
def test_put_preferences_normalizes_and_writes_whole(signed_in: Any) -> None:
    client, user = signed_in(
        preferences={"target_roles": ["Old"], "start_date": "Soon", "dealbreakers": ["Travel"]},
        interview_state={"version": 1, "skipped": ["salary_floor", "start_date", "constraints"]},
    )
    many = [f"Role {n}" for n in range(45)]
    response = client.put(
        "/api/preferences",
        {
            "target_roles": ["  Backend Engineer ", "backend engineer", "", "x" * 250, *many],
            "seniority": "mid",
            "locations": ["   "],
            "salary_floor": {"amount": 150000, "currency": " ", "period": "year"},
            "work_auth": "  " + "y" * 600,
            "start_date": "   ",
            "must_haves": ["Python", "python ", "PYTHON"],
            "unknown_key": "ignored",
        },
    )
    assert response.status_code == 200, response.content
    user.refresh_from_db()
    stored = user.preferences
    assert set(stored) == set(Preferences.model_fields)  # every key is written
    assert stored["target_roles"][:2] == ["Backend Engineer", "x" * 200]
    assert len(stored["target_roles"]) == 40
    assert stored["seniority"] == "mid"
    assert stored["locations"] is None
    assert stored["remote_pref"] is None
    assert stored["salary_floor"] == {"amount": 150000, "currency": "USD", "period": "year"}
    assert stored["work_auth"] == "y" * 500
    assert stored["start_date"] is None  # cleared: written whole, not merged
    assert stored["must_haves"] == ["Python"]
    assert stored["dealbreakers"] is None
    # Filled slots are no longer "declined"; still-empty ones stay declined.
    assert user.interview_state == {"version": 1, "skipped": ["start_date"]}


@pytest.mark.django_db
@pytest.mark.parametrize("amount", [0, -5])
def test_put_preferences_drops_a_non_positive_salary(signed_in: Any, amount: int) -> None:
    client, user = signed_in()
    body = {"salary_floor": {"amount": amount, "currency": "EUR", "period": "hour"}}
    assert client.put("/api/preferences", body).status_code == 200
    user.refresh_from_db()
    assert user.preferences["salary_floor"] is None


@pytest.mark.django_db
def test_put_preferences_keeps_a_fractional_salary(signed_in: Any) -> None:
    client, user = signed_in()
    body = {"salary_floor": {"amount": 62.5, "currency": "EUR", "period": "hour"}}
    assert client.put("/api/preferences", body).status_code == 200
    user.refresh_from_db()
    assert user.preferences["salary_floor"] == {"amount": 62.5, "currency": "EUR", "period": "hour"}


@pytest.mark.django_db
def test_put_preferences_validation_messages(signed_in: Any) -> None:
    client, user = signed_in(preferences={"target_roles": ["Kept"]})
    cases = {
        '{"seniority": "emperor"}': "Seniority could not be saved: that is not one of the options.",
        '{"target_roles": "engineer"}': "Target roles could not be saved: it should be a list.",
        '{"work_auth": 5}': "Work authorisation could not be saved: it should be text.",
        '{"dealbreakers": [1]}': (
            "Must-haves and dealbreakers could not be saved: it should be text."
        ),
        '{"salary_floor": {"amount": "lots", "currency": "USD", "period": "year"}}': (
            "Salary floor could not be saved: the amount should be a number."
        ),
        '{"salary_floor": {"amount": 1, "period": "year"}}': (
            "Salary floor could not be saved: the currency is missing."
        ),
        '{"salary_floor": {"amount": 1, "currency": "USD", "period": "week"}}': (
            "Salary floor could not be saved: that is not one of the options."
        ),
        "[1, 2]": "Those preferences could not be saved. Check the fields and try again.",
    }
    for body, message in cases.items():
        response = client.put("/api/preferences", body)
        assert response.status_code == 400, body
        assert response.json() == {"ok": False, "message": message}, body
    user.refresh_from_db()
    assert user.preferences == {"target_roles": ["Kept"]}


# --- PATCH /api/settings -------------------------------------------------------------------------


@pytest.mark.django_db
def test_patch_settings_merges_partial_updates(signed_in: Any) -> None:
    client, user = signed_in()
    first = client.patch("/api/settings", {"scoreThreshold": 70})
    assert first.status_code == 200, first.content
    assert first.json() == {
        "ok": True,
        "message": "Settings saved",
        "settings": {"defaultMode": "approve_each", "scoreThreshold": 70, "deepEvalTopN": 15},
    }
    second = client.patch("/api/settings", {"defaultMode": "autopilot", "deep_eval_top_n": 0})
    assert second.json()["settings"] == {
        "defaultMode": "autopilot",
        "scoreThreshold": 70,  # not reset by a save that did not send it
        "deepEvalTopN": 0,
    }
    user.refresh_from_db()
    assert user.settings == {"defaultMode": "autopilot", "scoreThreshold": 70, "deepEvalTopN": 0}
    assert client.patch("/api/settings", {}).json()["settings"]["scoreThreshold"] == 70


@pytest.mark.django_db
def test_patch_settings_rejects_out_of_range_and_unknown(signed_in: Any) -> None:
    client, user = signed_in(settings={"scoreThreshold": 40})
    message = "Those settings are out of range. Check the values and try again."
    for body in (
        {"scoreThreshold": 101},
        {"scoreThreshold": -1},
        {"deepEvalTopN": 61},
        {"scoreThreshold": 55.5},
        {"scoreThreshold": "55"},
        {"scoreThreshold": True},
        {"scoreThreshold": None},
        {"defaultMode": "yolo"},
        {"theme": "dark"},
    ):
        response = client.patch("/api/settings", body)
        assert response.status_code == 400, body
        assert response.json() == {"ok": False, "message": message}, body
    user.refresh_from_db()
    assert user.settings == {"scoreThreshold": 40}


@pytest.mark.django_db
def test_patch_settings_for_a_user_without_a_resume(signed_in: Any) -> None:
    """Settings live on the user row, so they always save (no "Upload your resume first")."""
    client, user = signed_in()
    assert client.patch("/api/settings", {"deepEvalTopN": 20}).status_code == 200
    assert User.objects.get(pk=user.pk).settings["deepEvalTopN"] == 20
