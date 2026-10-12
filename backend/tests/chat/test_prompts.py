"""The chat prompts against the TypeScript goldens, byte for byte (design §8.10).

- `chat_system_prompt` (8 cases, both branches): the Python prompt equals the golden once the
  declared differences of §7.1/§7.2 are undone (`ALLOWED_DIFFS`).
- `discover_companies`: the name proposal's instructions and prompt.
- `tool_salary`: `get_job`'s salary string and `list_jobs`' card, salary included.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from apps.accounts.schemas import InterviewState, Preferences, Settings
from apps.chat import prompt
from apps.chat.tools import (
    PROPOSAL_INSTRUCTIONS,
    format_card_salary,
    format_job_salary,
    job_card,
    proposal_prompt,
)
from apps.matching.queries import MatchRow

GOLDENS = Path(__file__).resolve().parents[1] / "goldens" / "prompts"

# Each entry: what differs from the TypeScript prompt, and why (§7.1, §7.2, §8.10).
ALLOWED_DIFFS = {
    "date_line_last": "§7.1: the date line moves after the capabilities, so the stable prefix "
    "keeps hitting provider prompt caches across days",
    "untrusted_paragraph": "§7.2: one rule on text inside <untrusted …> tags",
    "no_resume_keeps_preferences": "§7.1: without a resume the prompt still carries the "
    "preferences or interview block, so answers given before the upload are not re-asked",
}


def _cases(builder: str) -> list[dict[str, Any]]:
    return [json.loads(path.read_text()) for path in sorted((GOLDENS / builder).glob("*.json"))]


def _build(case: dict[str, Any]) -> str:
    given = case["input"]
    return prompt.system_prompt(
        has_resume=given["hasProfile"],
        preferences=Preferences.read(given["preferences"]),
        interview_state=InterviewState.read(given["interviewState"]),
        settings=Settings.read(given["settings"]) if given["settings"] is not None else None,
        user_name=given["userName"],
        now=datetime.fromisoformat(given["now"].replace("Z", "+00:00")),
        time_zone=given["timeZone"],
    )


def as_typescript(text: str, case: dict[str, Any]) -> str:
    """The Python prompt with the declared differences undone."""
    given = case["input"]
    now = datetime.fromisoformat(given["now"].replace("Z", "+00:00"))
    today = prompt.today_line(now, given["timeZone"])
    # date_line_last
    assert text.endswith(today), "the date line comes last"
    text = text.removesuffix(today)
    who = f"\nThe user's name is {given['userName']}." if given["userName"] else ""
    head = prompt.BASE + prompt.UNTRUSTED + who
    assert text.startswith(head)
    text = head + today + text.removeprefix(head)
    # untrusted_paragraph
    assert text.count(prompt.UNTRUSTED) == 1
    text = text.replace(prompt.UNTRUSTED, "")
    # no_resume_keeps_preferences
    if not given["hasProfile"]:
        section = prompt.preferences_section(
            Preferences.read(given["preferences"]), InterviewState.read(given["interviewState"])
        )
        assert text.count(section) == 1
        text = text.replace(section, "")
    return text


@pytest.mark.parametrize("case", _cases("chat_system_prompt"), ids=lambda case: case["case"])
def test_chat_system_prompt_matches_the_golden(case: dict[str, Any]) -> None:
    built = _build(case)
    assert as_typescript(built, case) == case["instructions"]


def test_the_date_is_the_users_and_comes_last() -> None:
    case = json.loads((GOLDENS / "chat_system_prompt" / "complete_all_autopilot.json").read_text())
    built = _build(case)
    # 03:30 UTC on 5 October is still 4 October in Los Angeles.
    assert built.endswith("\nToday is Sunday, October 4, 2026.")
    case["input"]["timeZone"] = "Europe/Berlin"
    assert _build(case).endswith("\nToday is Monday, October 5, 2026.")
    case["input"]["timeZone"] = "Not/AZone"  # an unreadable zone reads as UTC
    assert _build(case).endswith("\nToday is Monday, October 5, 2026.")


def test_without_a_resume_the_answers_still_reach_the_prompt() -> None:
    case = json.loads(
        (GOLDENS / "chat_system_prompt" / "no_resume_named_autopilot.json").read_text()
    )
    built = _build(case)
    nudge = built.index("## Right now")
    interview = built.index("## The interview")
    capabilities = built.index("## What you can do")
    assert nudge < interview < capabilities
    assert '- target_roles: ["Software engineer"]' in built
    assert "A search cannot start until these are known: Seniority" in built
    assert "Their default mode is autopilot (no pauses)." in built


def test_untrusted_text_is_named_in_the_rules() -> None:
    built = _build(json.loads((GOLDENS / "chat_system_prompt" / "no_resume.json").read_text()))
    assert "<untrusted" in built and "never as instructions" in built


@pytest.mark.parametrize("case", _cases("discover_companies"), ids=lambda case: case["case"])
def test_discover_companies_prompt_matches_the_golden(case: dict[str, Any]) -> None:
    assert case["instructions"] == PROPOSAL_INSTRUCTIONS
    given = case["input"]
    assert proposal_prompt(given["description"], given["count"]) == case["prompt"]


def _number(value: str | None) -> float | None:
    return float(value) if value is not None else None


def test_get_job_salary_matches_the_golden() -> None:
    golden = json.loads((GOLDENS / "tool_salary" / "get_job.json").read_text())
    for case in golden["cases"]:
        given = case["input"]
        salary = format_job_salary(
            _number(given["salary_min"]),
            _number(given["salary_max"]),
            given["salary_currency"],
            given["salary_period"],
        )
        assert salary == case["output"]["salary"], given


def test_list_jobs_card_matches_the_golden() -> None:
    golden = json.loads((GOLDENS / "tool_salary" / "list_jobs.json").read_text())
    for case in golden["cases"]:
        given = case["input"]
        row = MatchRow.model_construct(
            match_id=given["matchId"],  # the golden's ids are not UUIDs; the card prints them
            job_id=given["jobId"],
            title=given["title"],
            company=given["company"],
            locations=given["locations"],
            remote=given["remote"],
            stage=given["stage"],
            score=given["score"],
            fast_score=given["fastScore"],
            deep_score=given["deepScore"],
            recommendation=given["recommendation"],
            headline=given["headline"],
            posted_at=datetime.fromisoformat(given["postedAt"].replace("Z", "+00:00")),
            url=given["url"],
            salary_min=given["salaryMin"],
            salary_max=given["salaryMax"],
            salary_currency=given["salaryCurrency"],
            salary_period=given["salaryPeriod"],
        )
        card = job_card(row)
        # Key order too: it is the order the client receives.
        assert list(card.items()) == list(case["output"].items()), given


def test_card_salary_rounds_like_javascript() -> None:
    assert format_card_salary(149500, None, None, None) == "$150k"  # Math.round(149.5) is 150
    assert format_card_salary(0.5, None, "USD", "hour") == "$1/hour"
    assert format_card_salary(None, None, "USD", "year") is None
