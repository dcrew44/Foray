"""What one search is for (design §9.4, R15; `map/llm.md` §10; a port of
`src/lib/ranking/brief.ts`).

Saved preferences say what someone generally wants; a request often asks for something narrower
or different: "entry-level roles at Anthropic", "remote data jobs in fintech". The brief holds
only what the request itself states. A null field means "as saved". It overrides the
preferences for that one run and never changes them: **a search follows its request**, so every
search reader takes `apply_brief(saved, brief)`, never the saved preferences directly.

`parse_brief` and `describe_brief` also serve the live run card (`runs.live.search_summary`).
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict

from apps.accounts.schemas import Preferences, RemotePreference, Seniority
from apps.resumes.text import js_trim

__all__ = [
    "EMPTY_BRIEF",
    "SAVED_PREFERENCES_PROMPT",
    "SearchBrief",
    "apply_brief",
    "clean_brief",
    "describe_brief",
    "is_empty_brief",
    "join_or",
    "merge_brief",
    "parse_brief",
    "request_text",
]


class SearchBrief(BaseModel):
    # Every field is required and nullable: it is part of the `score` model's output schema
    # (`prefilter.DerivedSearch`), and OpenAI's strict mode refuses optional properties.
    model_config = ConfigDict(extra="ignore")

    roles: list[str] | None  # kinds of role asked for, as short titles ("software engineer")
    seniority: Seniority | None
    locations: list[str] | None
    remote: RemotePreference | None
    companies: list[str] | None  # companies the search is limited to, as the user named them
    focus: str | None  # anything else that should shape the ranking, in one sentence

    def dump(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class _PartialBrief(BaseModel):
    # `SearchBrief.partial()`: any field may be missing, none may be the wrong type.
    model_config = ConfigDict(extra="ignore")

    roles: list[str] | None = None
    seniority: Seniority | None = None
    locations: list[str] | None = None
    remote: RemotePreference | None = None
    companies: list[str] | None = None
    focus: str | None = None


EMPTY_BRIEF = SearchBrief(
    roles=None, seniority=None, locations=None, remote=None, companies=None, focus=None
)

# What a search started without a request (the Home button) records as its prompt.
SAVED_PREFERENCES_PROMPT = "Search with my saved preferences"


def request_text(prompt: str | None) -> str | None:
    """The request worth reading, or None when there is none beyond the saved preferences."""
    text = js_trim(prompt) if prompt is not None else ""
    return text if text and text != SAVED_PREFERENCES_PROMPT else None


def _partial(value: Any) -> _PartialBrief | None:
    if isinstance(value, SearchBrief | _PartialBrief):
        return _PartialBrief.model_validate(value.model_dump())
    try:
        return _PartialBrief.model_validate({} if value is None else value)
    except ValueError, TypeError:  # pydantic's ValidationError is a ValueError
        return None


def parse_brief(value: Any) -> SearchBrief:
    """A stored brief (or a chat agent's hints), read leniently: anything unreadable means "as
    saved". Never raises."""
    parsed = _partial(value)
    return clean_brief(parsed) if parsed is not None else EMPTY_BRIEF


def _clean_list(values: list[str] | None) -> list[str] | None:
    seen: set[str] = set()
    kept: list[str] = []
    for value in values or []:
        text = js_trim(value)
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        kept.append(text)
    return kept or None


def clean_brief(brief: Any) -> SearchBrief:
    """Trims strings, drops blanks and duplicates (case-insensitively, first spelling kept), and
    turns an empty list into None, so "no companies" and "companies: []" mean the same thing.
    Takes a brief, a partial one, or a mapping of brief fields."""
    parsed = _partial(brief)
    if parsed is None:
        return EMPTY_BRIEF
    focus = js_trim(parsed.focus) if parsed.focus is not None else ""
    return SearchBrief(
        roles=_clean_list(parsed.roles),
        seniority=parsed.seniority,
        locations=_clean_list(parsed.locations),
        remote=parsed.remote,
        companies=_clean_list(parsed.companies),
        focus=focus or None,
    )


def merge_brief(derived: Any, hints: Any) -> SearchBrief:
    """The derived brief with the chat agent's structured hints laid over it, field by field.
    The hints come from a model that saw the whole conversation, so where both say something,
    the hint wins."""
    base = clean_brief(derived)
    over = clean_brief(hints if hints is not None else {})
    return SearchBrief(
        roles=over.roles if over.roles is not None else base.roles,
        seniority=over.seniority if over.seniority is not None else base.seniority,
        locations=over.locations if over.locations is not None else base.locations,
        remote=over.remote if over.remote is not None else base.remote,
        companies=over.companies if over.companies is not None else base.companies,
        focus=over.focus if over.focus is not None else base.focus,
    )


def is_empty_brief(brief: SearchBrief) -> bool:
    return all(value is None for value in brief.model_dump().values())


def apply_brief(preferences: Preferences, brief: SearchBrief) -> Preferences:
    """The preferences this search runs on: the brief where it says something, the saved ones
    elsewhere. `companies` and `focus` are not preferences; callers read them from the brief."""
    return preferences.model_copy(
        update={
            "target_roles": brief.roles if brief.roles is not None else preferences.target_roles,
            "seniority": brief.seniority if brief.seniority is not None else preferences.seniority,
            "locations": brief.locations if brief.locations is not None else preferences.locations,
            "remote_pref": brief.remote if brief.remote is not None else preferences.remote_pref,
        }
    )


LEVEL: dict[str, str] = {
    "intern": "Internship",
    "new_grad": "New-grad",
    "entry": "Entry-level",
    "mid": "Mid-level",
    "senior": "Senior",
    "staff_plus": "Staff-level and above",
}

# A role that already names its level. JavaScript's `\b` is ASCII-only, spelled out here.
LEVEL_WORD = re.compile(
    r"^(senior|sr\.?|staff|principal|lead|junior|jr\.?|entry|new[- ]grad|intern|mid)"
    r"(?:(?<=[A-Za-z0-9_])(?![A-Za-z0-9_])|(?<![A-Za-z0-9_])(?=[A-Za-z0-9_]))",
    re.IGNORECASE | re.ASCII,
)

REMOTE: dict[str, str | None] = {
    "remote": "remote only",
    "hybrid": "hybrid",
    "onsite": "on site",
    "any": None,
}


def join_or(items: list[str]) -> str:
    """`a`, `a or b`, `a, b or c`."""
    if len(items) <= 1:
        return items[0] if items else ""
    if len(items) == 2:
        return f"{items[0]} or {items[1]}"
    return f"{', '.join(items[:-1])} or {items[-1]}"


def describe_brief(brief: SearchBrief) -> str | None:
    """The brief in one line, for the run card and the activity feed: "Entry-level software
    engineer roles at Anthropic, in San Francisco Bay Area". None when the brief adds nothing to
    the saved preferences (`focus` alone does not count)."""
    roles, seniority, locations = brief.roles, brief.seniority, brief.locations
    remote, companies = brief.remote, brief.companies
    if not roles and not seniority and not locations and not remote and not companies:
        return None

    what = f"{join_or(roles)} roles" if roles else "roles"
    # "Senior Backend Engineer roles", not "Senior Senior Backend Engineer roles".
    levelled = any(LEVEL_WORD.search(role) for role in roles or [])
    if seniority and not levelled:
        text = f"{LEVEL[seniority]} {what}"
    else:
        text = what[:1].upper() + what[1:]

    if companies:
        text += f" at {join_or(companies)}"
    if locations:
        text += f", in {join_or(locations)}"
    mode = REMOTE[remote] if remote else None
    if mode:
        text += f", {mode}"
    return text
