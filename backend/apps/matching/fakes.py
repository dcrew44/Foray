"""The `LLM_FAKE` overrides this lane owns (design §8.9): `score` and `evaluate`.

Deterministic, free, and good enough for a search to run end to end on fakes: it derives title
terms, scores a spread of postings (some archived, some worth a deep look) and evaluates them
into every band, so both gates have something to propose.

- `score`, two output types share the purpose:
  - `DerivedSearch` (the search's brief and title terms): the brief is empty (the hints still
    apply on top), and the title terms are the roles the prompt names: the resolved roles when
    the chat agent set them, else the saved target roles, lowercased.
  - `FastScore`: a score from 40 to 99 derived from a stable hash of the posting's title,
    company and location lines (the prompt carries no job id), with the fit reasons naming the
    posting.
- `evaluate` (`DeepEval`): a templated review whose score (50 to 99) comes from the same kind of
  hash, with the recommendation in the band the rubric gives that score, citing the resume's
  first item when there is one.

Each override answers only its own output type (told apart by the output schema's properties)
and returns None for anything else, so the schema-driven default still answers the rest.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo

from apps.llm import fake

__all__ = [
    "deep_eval_reply",
    "derived_search_reply",
    "fast_score_reply",
    "fake_deep_score",
    "fake_fast_score",
    "recommendation_for",
]


def _properties(info: AgentInfo) -> set[str]:
    schema = fake.output_schema(info) or {}
    return set(schema.get("properties", {}))


def _line(text: str, label: str) -> str:
    match = re.search(rf"^{re.escape(label)}: (.*)$", text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def _posting_key(prompt: str) -> str:
    return "|".join(_line(prompt, label) for label in ("Title", "Company", "Location"))


def _hash(key: str) -> int:
    return int(hashlib.sha1(key.encode()).hexdigest()[:8], 16)


def fake_fast_score(prompt: str) -> int:
    """40-99, stable per posting."""
    return 40 + _hash(_posting_key(prompt)) % 60


def fake_deep_score(prompt: str) -> int:
    """50-99, stable per posting."""
    return 50 + _hash("deep|" + _posting_key(prompt)) % 50


def recommendation_for(score: int) -> str:
    if score >= 85:
        return "strong"
    if score >= 70:
        return "apply"
    if score >= 50:
        return "maybe"
    return "skip"


# --- score ---------------------------------------------------------------------------------------

_SAVED = re.compile(r"\ASaved preferences:\n(\{.*?\n\})", re.DOTALL)
_RESOLVED = re.compile(r"Already resolved for this search[^\n]*\n(\{.*?\n\})", re.DOTALL)


def _json_block(pattern: re.Pattern[str], text: str) -> dict[str, Any]:
    found = pattern.search(text)
    if not found:
        return {}
    try:
        value = json.loads(found.group(1))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def derived_search_reply(messages: list[ModelMessage], info: AgentInfo) -> dict[str, Any] | None:
    if "title_include" not in _properties(info):
        return None
    prompt = fake.latest_user_text(messages)
    roles = _json_block(_RESOLVED, prompt).get("roles") or _json_block(_SAVED, prompt).get(
        "target_roles"
    )
    titles = [role.lower() for role in roles or [] if isinstance(role, str) and role.strip()]
    return {
        "brief": {
            "roles": None,
            "seniority": None,
            "locations": None,
            "remote": None,
            "companies": None,
            "focus": None,
        },
        "title_include": titles or ["software engineer"],
        "title_exclude": [],
        "keywords": [],
    }


def fast_score_reply(messages: list[ModelMessage], info: AgentInfo) -> dict[str, Any] | None:
    if "fit_reasons" not in _properties(info):
        return None
    prompt = fake.latest_user_text(messages)
    title = _line(prompt, "Title") or "this role"
    return {
        "score": fake_fast_score(prompt),
        "fit_reasons": [f"Fake triage of {title}: no model was called."],
        "red_flags": [],
        "seniority": "unclear",
        "hard_requirement_misses": [],
    }


# --- evaluate ------------------------------------------------------------------------------------

_FACT_ID = re.compile(r"^\[([^\]\n]+)\] ", re.MULTILINE)


def deep_eval_reply(messages: list[ModelMessage], info: AgentInfo) -> dict[str, Any] | None:
    if "matched_requirements" not in _properties(info):
        return None
    prompt = fake.latest_user_text(messages)
    title = _line(prompt, "Title") or "this role"
    company = _line(prompt, "Company") or "this company"
    score = fake_deep_score(prompt)
    cited = _FACT_ID.findall(info.instructions or "")[:1]
    return {
        "matched_requirements": [
            {
                "requirement": f"Experience relevant to {title}",
                "evidence": "Your most recent role on the resume.",
                "fact_ids": cited,
                "strength": "partial",
            }
        ],
        "gaps": [],
        "seniority_fit": "unclear",
        "location_fit": "unclear",
        "salary_fit": "unstated",
        "risk_flags": [],
        "talking_points": [f"Lead with your most recent role when applying to {company}."],
        "summary": (
            f"This is a fake review of {title} at {company}, written without a model. "
            "It exists to exercise the flow."
        ),
        "score": score,
        "recommendation": recommendation_for(score),
    }


fake.register("score", derived_search_reply)
fake.register("score", fast_score_reply)
fake.register("evaluate", deep_eval_reply)
