"""The interview, onboarding and the writers of the user's own JSON columns (design §5.3;
`map/llm.md` §21, `map/_gaps.md` §3.7). A port of `src/lib/profile/interview.ts`,
`onboarding.ts` and `settings.ts`, and of `savePreferencesAction` / `saveSettingsAction`.

What Foray needs to know before it can search on someone's behalf is one versioned list,
`SLOTS`, rather than questions scattered through a prompt: the interview is going to grow, and a
stored `interview_state` written against version 1 has to stay readable when it does. Adding a slot
is an edit here plus a version bump (`schemas.INTERVIEW_VERSION`), and nothing else. The profile
page serves `SLOTS`, so the frontend keeps no copy.

Preferences, interview state and settings live on `users` (§1.2), so they can be saved before a
resume exists: the "Upload your resume first" dead end is gone.
"""

from __future__ import annotations

import math
from typing import Any, Literal

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.schemas import InterviewState, Preferences, SalaryFloor, Settings
from apps.core.jsonio import js_json
from apps.core.router import ApiModel
from apps.resumes.queries import latest_base_resume
from apps.resumes.schema import Resume

SlotId = Literal[
    "target_roles",
    "seniority",
    "locations",
    "remote_pref",
    "salary_floor",
    "work_auth",
    "start_date",
    "constraints",
]

PreferenceField = Literal[
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


class Slot(ApiModel):
    id: SlotId
    label: str
    # What the agent is trying to find out: not a script to read verbatim.
    intent: str
    # Which preference fields this slot fills. `constraints` covers two, because must-haves and
    # dealbreakers are one conversational question.
    fields: list[PreferenceField]
    # A search refuses to start without these. The rest sharpen ranking but should never block
    # someone who would rather just look.
    required: bool


SLOTS: tuple[Slot, ...] = (
    Slot(
        id="target_roles",
        label="Target roles",
        intent=(
            "What kind of role they want, in their own words — job titles or a description of "
            "the work."
        ),
        fields=["target_roles"],
        required=True,
    ),
    Slot(
        id="seniority",
        label="Seniority",
        intent=(
            "What level they are aiming at: internship, new grad, entry, mid, senior, or staff "
            "and above."
        ),
        fields=["seniority"],
        required=True,
    ),
    Slot(
        id="locations",
        label="Locations",
        intent=(
            "Where they want to work — metros, cities or regions. 'Anywhere in the US' is a "
            "valid answer."
        ),
        fields=["locations"],
        required=True,
    ),
    Slot(
        id="remote_pref",
        label="Remote preference",
        intent="Remote, hybrid, onsite, or no strong preference.",
        fields=["remote_pref"],
        required=True,
    ),
    Slot(
        id="work_auth",
        label="Work authorisation",
        intent=(
            "Whether they can work in their target countries without sponsorship, or need it. "
            "This changes which postings are worth showing at all."
        ),
        fields=["work_auth"],
        required=True,
    ),
    Slot(
        id="salary_floor",
        label="Salary floor",
        intent=(
            "The lowest compensation worth their time. They may decline to say, which is fine."
        ),
        fields=["salary_floor"],
        required=False,
    ),
    Slot(
        id="start_date",
        label="Start date",
        intent="When they could start — a date, or 'immediately'.",
        fields=["start_date"],
        required=False,
    ),
    Slot(
        id="constraints",
        label="Must-haves and dealbreakers",
        intent=(
            "Anything a role must have, and anything that rules one out — company size, "
            "industry, on-call, travel, return-to-office."
        ),
        fields=["must_haves", "dealbreakers"],
        required=False,
    ),
)

SLOT_BY_ID: dict[str, Slot] = {slot.id: slot for slot in SLOTS}


def slot_for_field(field: str) -> Slot | None:
    """The slot that owns a preference field (`must_haves` → `constraints`)."""
    return next((slot for slot in SLOTS if field in slot.fields), None)


def is_filled(slot: Slot, preferences: Preferences) -> bool:
    """A slot counts as filled when **any** field it owns has a value; an empty list is no
    value. (The TypeScript doc comment said "every"; its code and tests said "some".)"""
    for field in slot.fields:
        value = getattr(preferences, field)
        if value is None:
            continue
        if isinstance(value, list) and not value:
            continue
        return True
    return False


class InterviewProgress(ApiModel):
    filled: list[Slot]
    # Still worth asking about: neither answered nor explicitly declined.
    outstanding: list[Slot]
    # Required slots still missing: what blocks a search.
    blocking: list[Slot]
    answered: int
    total: int
    complete: bool


def interview_progress(preferences: Preferences, state: InterviewState) -> InterviewProgress:
    skipped = set(state.skipped)
    filled = [slot for slot in SLOTS if is_filled(slot, preferences)]
    outstanding = [
        slot for slot in SLOTS if not is_filled(slot, preferences) and slot.id not in skipped
    ]
    # Declining is allowed; declining your way into a search is not.
    blocking = [slot for slot in SLOTS if slot.required and not is_filled(slot, preferences)]
    return InterviewProgress(
        filled=filled,
        outstanding=outstanding,
        blocking=blocking,
        # Declining to answer is progress: the checklist reaches the end for someone who does
        # not want to state a salary floor.
        answered=len(SLOTS) - len(outstanding),
        total=len(SLOTS),
        complete=not outstanding,
    )


def _js_value(value: Any) -> Any:
    # JSON.stringify prints an integral number without ".0".
    if isinstance(value, float) and value.is_integer() and math.isfinite(value):
        return int(value)
    if isinstance(value, dict):
        return {key: _js_value(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_js_value(inner) for inner in value]
    return value


def describe_preferences(preferences: Preferences) -> str:
    """The current answers for the agents' instructions (the deep review and chat prompts): one
    `- <field>: <JSON>` line per recorded field, in slot order."""
    stored = preferences.dump()
    lines = []
    for slot in SLOTS:
        for field in slot.fields:
            value = stored.get(field)
            if value is None or (isinstance(value, list) and not value):
                continue
            lines.append(f"- {field}: {js_json(_js_value(value))}")
    return "\n".join(lines) if lines else "- nothing recorded yet"


# --- onboarding ----------------------------------------------------------------------------------


class OnboardingState(ApiModel):
    """Where one user stands between signing up and a first search: one answer, read the same way
    by Home, Profile, Matches and the agent (`map/_gaps.md` §3.7, minus `hasProfile`)."""

    has_resume: bool
    # The newest base resume: the one source of resume facts (R14), not a mirror.
    resume: Resume | None
    preferences: Preferences
    progress: InterviewProgress
    settings: Settings
    # A search can start: a resume is in, and every required preference is known.
    ready: bool


def onboarding_state(user: User) -> OnboardingState:
    """One query (the latest base resume): the JSON columns are on the user row already."""
    base = latest_base_resume(user.pk)
    resume = base.resume if base is not None else None
    has_resume = resume is not None and (
        len(resume.work) + len(resume.education) + len(resume.skills) > 0
    )
    preferences = Preferences.read(user.preferences)
    progress = interview_progress(preferences, InterviewState.read(user.interview_state))
    return OnboardingState(
        has_resume=has_resume,
        resume=resume,
        preferences=preferences,
        progress=progress,
        settings=Settings.read(user.settings),
        ready=has_resume and not progress.blocking,
    )


# --- writers -------------------------------------------------------------------------------------

LIST_ITEM_MAX = 200
LIST_MAX = 40
TEXT_MAX = 500


def _clean_list(items: list[str] | None) -> list[str] | None:
    if not items:
        return None
    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in items:
        # Cut by code point, never mid-character (JavaScript's slice could split a surrogate
        # pair, which Postgres's jsonb then refuses).
        item = raw.strip()[:LIST_ITEM_MAX]
        key = item.lower()
        if not item or key in seen:
            continue
        seen.add(key)
        cleaned.append(item)
    cleaned = cleaned[:LIST_MAX]
    return cleaned or None


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip()[:TEXT_MAX] or None


def normalize_preferences(preferences: Preferences) -> Preferences:
    """Trimmed and de-duplicated (case-insensitively, first wins); empty answers become None
    ("not known"); a salary floor only with a positive, finite amount."""
    salary = preferences.salary_floor
    floor = None
    if salary is not None and math.isfinite(salary.amount) and salary.amount > 0:
        floor = SalaryFloor(
            amount=salary.amount, currency=salary.currency.strip() or "USD", period=salary.period
        )
    return Preferences(
        target_roles=_clean_list(preferences.target_roles),
        seniority=preferences.seniority,
        locations=_clean_list(preferences.locations),
        remote_pref=preferences.remote_pref,
        salary_floor=floor,
        work_auth=_clean_text(preferences.work_auth),
        start_date=_clean_text(preferences.start_date),
        must_haves=_clean_list(preferences.must_haves),
        dealbreakers=_clean_list(preferences.dealbreakers),
    )


def save_preferences(user_id: str, preferences: Preferences) -> Preferences:
    """The Preferences form's save: written whole rather than merged, because the form shows every
    field and an emptied one means "clear this". A slot the user now fills is no longer
    "declined", so the interview state stays truthful for the agent that reads it. Works with or
    without a resume. Returns what was stored."""
    normalized = normalize_preferences(preferences)
    with transaction.atomic():
        row = User.objects.select_for_update().only("interview_state").get(pk=user_id)
        state = InterviewState.read(row.interview_state)
        state.skipped = [
            slot_id
            for slot_id in state.skipped
            if (slot := SLOT_BY_ID.get(slot_id)) is None or not is_filled(slot, normalized)
        ]
        User.objects.filter(pk=user_id).update(
            preferences=normalized.dump(),
            interview_state=state.dump(),
            updated_at=timezone.now(),
        )
    return normalized


def update_settings(user_id: str, patch: dict[str, Any]) -> Settings:
    """Merge a partial update (field names as keys, already validated) into the stored settings
    and store the full result. Raises pydantic's `ValidationError` if the merge is out of
    range."""
    with transaction.atomic():
        row = User.objects.select_for_update().only("settings").get(pk=user_id)
        current = Settings.read(row.settings)
        merged = Settings.model_validate({**current.model_dump(), **patch})
        User.objects.filter(pk=user_id).update(settings=merged.dump(), updated_at=timezone.now())
    return merged
