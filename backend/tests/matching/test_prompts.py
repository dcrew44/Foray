"""The ranking prompts and the search's wording against the TypeScript goldens, byte for byte
(design §8.10, §12.1).

Prompt builders: `deriveSearch`, the fast score and the deep evaluation, each `{instructions,
prompt}` exactly as the model received them. `ALLOWED_DIFFS` lists every declared difference with
its reason; nothing else may differ. Strings: `summarizeSearchRun`, `describeEvaluations`.

The goldens give salaries as the strings Postgres returned for the legacy `numeric` columns; the
mirror now stores `float8`, so they are read as floats here, as the new columns read them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from apps.accounts.schemas import Preferences
from apps.matching.brief import EMPTY_BRIEF, apply_brief, parse_brief
from apps.matching.deep_eval import EvalCandidate, EvalPosting, eval_instructions, eval_prompt
from apps.matching.evaluate import describe_evaluations
from apps.matching.fast_score import ScorePosting, posting_prompt, score_instructions
from apps.matching.prefilter import SEARCH_INSTRUCTIONS, derive_search_prompt
from apps.matching.search import summarize_search_run
from apps.resumes.schema import Resume, fact_ids
from apps.resumes.text import resume_to_text
from tests.matching.conftest import GOLDENS, golden

# builder -> case -> part ("instructions" or "prompt") -> [(TypeScript text, Python text)]
ALLOWED_DIFFS: dict[str, dict[str, dict[str, list[tuple[str, str]]]]] = {
    # §8.10 / bug #92: a missing maximum prints the minimum alone, not `150000–null`.
    "fast_score": {
        "missing_max_no_resume": {"prompt": [("Pay: 150000–null", "Pay: 150000")]},
    },
}


def _cases(builder: str) -> list[str]:
    return sorted(path.stem for path in (GOLDENS / "prompts" / builder).glob("*.json"))


def _declared(builder: str, case: str, part: str, text: str) -> str:
    for before, after in ALLOWED_DIFFS.get(builder, {}).get(case, {}).get(part, []):
        assert text.count(before) == 1, (builder, case, before)
        text = text.replace(before, after)
    return text


def _number(value: Any) -> float | None:
    return None if value is None else float(value)


def _when(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def test_every_builder_has_goldens() -> None:
    for builder in ("derive_search", "fast_score", "deep_eval"):
        assert _cases(builder), builder


@pytest.mark.parametrize("case", _cases("derive_search"))
def test_derive_search_prompt(case: str) -> None:
    expected = golden(f"prompts/derive_search/{case}.json")
    given = expected["input"]
    hints = parse_brief(given["hints"]) if given["hints"] is not None else None
    assert expected["instructions"] == SEARCH_INSTRUCTIONS
    prompt = derive_search_prompt(Preferences.read(given["preferences"]), given["request"], hints)
    assert prompt == _declared("derive_search", case, "prompt", expected["prompt"])


@pytest.mark.parametrize("case", _cases("fast_score"))
def test_fast_score_prompt(case: str) -> None:
    expected = golden(f"prompts/fast_score/{case}.json")
    given = expected["input"]
    posting = given["posting"]
    resume = Resume.read(given["resume"])
    instructions = score_instructions(
        Preferences.read(given["preferences"]), given["focus"], resume
    )
    prompt = posting_prompt(
        ScorePosting(
            id=posting["id"],
            title=posting["title"],
            company=posting["company_name"],
            locations=posting["locations"],
            remote=posting["remote"],
            salary_min=_number(posting["salary_min"]),
            salary_max=_number(posting["salary_max"]),
            description=posting["description_text"],
        )
    )
    assert instructions == _declared("fast_score", case, "instructions", expected["instructions"])
    assert prompt == _declared("fast_score", case, "prompt", expected["prompt"])


@pytest.mark.parametrize("case", _cases("deep_eval"))
def test_deep_eval_prompt(case: str) -> None:
    expected = golden(f"prompts/deep_eval/{case}.json")
    given = expected["input"]
    resume = Resume.model_validate(given["resume"])
    brief = parse_brief(given["brief"]) if given["brief"] is not None else EMPTY_BRIEF
    candidate = EvalCandidate(
        resume_id="resume-1",
        resume_version=1,
        resume_text=resume_to_text(resume),
        preferences=apply_brief(Preferences.read(given["preferences"]), brief),
        focus=brief.focus,
        fact_ids=tuple(fact_ids(resume)),
    )
    posting = given["posting"]
    prompt = eval_prompt(
        EvalPosting(
            title=posting["title"],
            company=posting["company"],
            locations=posting["locations"],
            remote=posting["remote"],
            employment_type=posting["employmentType"],
            salary_min=_number(posting["salaryMin"]),
            salary_max=_number(posting["salaryMax"]),
            salary_currency=posting["salaryCurrency"],
            salary_period=posting["salaryPeriod"],
            posted_at=_when(posting["postedAt"]),
            description=posting["description"],
        ),
        _when(given["now"]),
    )
    assert eval_instructions(candidate) == _declared(
        "deep_eval", case, "instructions", expected["instructions"]
    )
    assert prompt == _declared("deep_eval", case, "prompt", expected["prompt"])


@pytest.mark.parametrize("case", golden("strings/summarize_search_run.json"))
def test_summarize_search_run(case: dict[str, Any]) -> None:
    given = case["input"]
    assert (
        summarize_search_run(
            candidates=given["candidates"],
            new_matches=given["newMatches"],
            scored=given["scored"],
            auto_archived=given["autoArchived"],
            evaluated=given["evaluated"],
            shortlisted=given["shortlisted"],
            recommended=given["recommended"],
            strong=given["strong"],
            good=given["good"],
        )
        == case["output"]
    )


@dataclass(frozen=True)
class Verdict:
    score: int
    recommendation: str


@dataclass(frozen=True)
class Evaluated:
    title: str
    company: str
    evaluation: Verdict


@pytest.mark.parametrize("case", golden("strings/describe_evaluations.json"))
def test_describe_evaluations(case: dict[str, Any]) -> None:
    evaluations = [
        Evaluated(item["title"], item["company"], Verdict(**item["evaluation"]))
        for item in case["input"]["evaluations"]
    ]
    assert describe_evaluations(evaluations, case["input"]["failed"]) == case["output"]


def test_goldens_directory_is_the_shared_one() -> None:
    assert Path(GOLDENS).name == "goldens"
