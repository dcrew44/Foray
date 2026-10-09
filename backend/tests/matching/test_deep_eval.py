"""The deep evaluation's pure parts (the prompt, the citation check, the cost quote) and one full
pass through `structured()` with a fake model, so no test here spends money (a port of
`src/test/unit/deep-eval.test.ts`; no `.env` dependency: test settings price
`openai:gpt-5.6-terra`).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from apps.accounts.schemas import Preferences
from apps.core.db import db
from apps.llm.context import llm_context
from apps.llm.models import LlmCall
from apps.matching.deep_eval import (
    EVAL_OUTPUT_TOKENS,
    MAX_DESCRIPTION_CHARS,
    EvalCandidate,
    EvalPosting,
    cost_phrase,
    estimate_eval_cost,
    estimate_eval_cost_for,
    eval_instructions,
    eval_prompt,
    run_deep_eval,
    sanitize_deep_eval,
    strip_fact_ids,
)
from apps.matching.schemas import DeepEval
from apps.resumes.schema import Resume, fact_ids
from apps.resumes.text import resume_to_text
from tests.fakes import fake_models

RESUME = Resume.model_validate(
    {
        "basics": {"name": "Ada Example", "links": []},
        "work": [
            {
                "id": "work-1",
                "company": "Initech",
                "position": "Backend Engineer",
                "startDate": "2022-01",
                "highlights": ["Built the billing service in TypeScript and Postgres"],
            }
        ],
        "education": [
            {
                "id": "edu-1",
                "institution": "State University",
                "area": "Computer Science",
                "studyType": "BS",
            }
        ],
        "projects": [{"id": "proj-1", "name": "Queue visualiser", "highlights": []}],
        "skills": [{"id": "skill-1", "name": "TypeScript", "category": "Languages"}],
    }
)

CANDIDATE = EvalCandidate(
    resume_id="resume-1",
    resume_version=1,
    resume_text=resume_to_text(RESUME),
    preferences=Preferences(target_roles=["Backend engineer"], locations=["San Francisco, CA"]),
    focus=None,
    fact_ids=tuple(fact_ids(RESUME)),
)

POSTING = EvalPosting(
    title="Backend Engineer, Payments",
    company="Stripe",
    locations=["San Francisco, CA"],
    remote="hybrid",
    employment_type="full-time",
    salary_min=150000.0,
    salary_max=190000.0,
    salary_currency="USD",
    salary_period="year",
    posted_at=datetime(2026, 9, 10, tzinfo=UTC),
    description="You will build payment systems in TypeScript.",
)

EVALUATION = DeepEval.model_validate(
    {
        "matched_requirements": [
            {
                "requirement": "Backend experience in TypeScript",
                "evidence": "Built the billing service in TypeScript and Postgres",
                "fact_ids": ["work-1", "[skill-1]", "invented-99", "work-1"],
                "strength": "strong",
            },
            {
                "requirement": "A CS degree",
                "evidence": "BS in Computer Science",
                "fact_ids": ["made-up"],
                "strength": "strong",
            },
        ],
        "gaps": [
            {"gap": "Payments domain", "severity": "learnable", "note": "Adjacent billing work."}
        ],
        "seniority_fit": "match",
        "location_fit": "match",
        "salary_fit": "unstated",
        "risk_flags": [],
        "talking_points": ["Lead with the billing service."],
        "summary": "A direct fit for the backend work, with billing close to payments.",
        "score": 84,
        "recommendation": "apply",
    }
)


# --- citations -----------------------------------------------------------------------------------


def test_keeps_real_fact_ids_forgives_brackets_drops_invented_ones_and_duplicates() -> None:
    clean = sanitize_deep_eval(EVALUATION, CANDIDATE.fact_ids)
    assert clean.matched_requirements[0].fact_ids == ["work-1", "skill-1"]
    # A requirement whose citations were all invented keeps its evidence but cites nothing.
    assert clean.matched_requirements[1].fact_ids == []
    assert clean.matched_requirements[1].evidence == "BS in Computer Science"


def test_changes_nothing_but_the_citations() -> None:
    clean = sanitize_deep_eval(EVALUATION, CANDIDATE.fact_ids)
    assert clean.model_copy(update={"matched_requirements": []}) == EVALUATION.model_copy(
        update={"matched_requirements": []}
    )


ID1 = "3d987730-884c-4e77-b414-92f932ccc77d"
ID2 = "71ac0b2e-1111-4222-8333-944455556666"
IDS = (ID1, ID2, "work-1")


def test_strips_bracketed_ids_from_every_prose_field_and_keeps_the_sentence() -> None:
    dirty = DeepEval.model_validate(
        {
            **EVALUATION.model_dump(),
            "matched_requirements": [
                {
                    "requirement": f"Event pipelines [{ID1}]",
                    "evidence": f"Cut dropped events by 94% [{ID1}] and led the migration "
                    f"[{ID2}, {ID1}].",
                    "fact_ids": [],
                    "strength": "strong",
                }
            ],
            "gaps": [
                {
                    "gap": f"Kafka [{ID2}]",
                    "severity": "learnable",
                    "note": f"Adjacent to [{ID2}] work.",
                }
            ],
            "risk_flags": [f"[{ID1}]", "Contract-to-hire"],
            "talking_points": [
                f"Lead with the migration [{ID2}].",
                "Mention the billing work [work-1]",
            ],
            "summary": f"Strong backend fit [{ID1}]. Payments is adjacent.",
        }
    )
    clean = sanitize_deep_eval(dirty, IDS)

    assert clean.summary == "Strong backend fit. Payments is adjacent."
    assert clean.matched_requirements[0].requirement == "Event pipelines"
    assert (
        clean.matched_requirements[0].evidence == "Cut dropped events by 94% and led the migration."
    )
    # Ids cited only in the evidence prose become citations, not losses.
    assert clean.matched_requirements[0].fact_ids == [ID1, ID2]
    assert clean.gaps[0].model_dump() == {
        "gap": "Kafka",
        "severity": "learnable",
        "note": "Adjacent to work.",
    }
    # A flag that was nothing but an id is dropped rather than shown empty.
    assert clean.risk_flags == ["Contract-to-hire"]
    assert clean.talking_points == ["Lead with the migration.", "Mention the billing work"]


def test_does_not_cite_a_bracketed_id_the_resume_does_not_have() -> None:
    stranger = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    dirty = EVALUATION.model_copy(
        update={
            "matched_requirements": [
                EVALUATION.matched_requirements[0].model_copy(
                    update={
                        "requirement": "Go",
                        "evidence": f"Wrote Go [{stranger}]",
                        "fact_ids": [],
                    }
                )
            ]
        }
    )
    clean = sanitize_deep_eval(dirty, IDS)
    assert clean.matched_requirements[0].evidence == "Wrote Go"
    assert clean.matched_requirements[0].fact_ids == []


def test_leaves_ordinary_brackets_alone() -> None:
    assert strip_fact_ids("Uses [Go] and TypeScript [2021–2023]") == (
        "Uses [Go] and TypeScript [2021–2023]"
    )


# --- the prompt ----------------------------------------------------------------------------------


def test_puts_rubric_preferences_and_the_resume_with_ids_in_the_stable_half() -> None:
    instructions = eval_instructions(CANDIDATE)
    assert "[work-1] Backend Engineer — Initech" in instructions
    assert "[edu-1]" in instructions
    assert "target_roles" in instructions
    assert "Stripe" not in instructions
    focused = eval_instructions(replace(CANDIDATE, focus="fintech only"))
    assert "\n- this search: fintech only\n\nResume" in focused


def test_describes_the_posting_with_pay_and_age() -> None:
    prompt = eval_prompt(POSTING, datetime(2026, 9, 24, 12, tzinfo=UTC))
    assert "Title: Backend Engineer, Payments" in prompt
    assert "Pay: 150,000–190,000 USD per year" in prompt
    assert "Posted: 2026-09-10 (14 days ago)" in prompt
    assert "Today: 2026-09-24" in prompt


def test_says_unstated_and_reads_a_long_description_only_so_far() -> None:
    long = replace(
        POSTING,
        salary_min=None,
        salary_max=None,
        posted_at=None,
        description="x" * (MAX_DESCRIPTION_CHARS + 500),
    )
    prompt = eval_prompt(long)
    assert "Pay: unstated" in prompt
    assert "Posted: unknown" in prompt
    assert f"{'x' * MAX_DESCRIPTION_CHARS}\n[Description cut at 12,000 characters.]" in prompt
    assert "x" * (MAX_DESCRIPTION_CHARS + 1) not in prompt


# --- the cost quote ------------------------------------------------------------------------------


def test_prices_input_from_characters_and_output_from_the_allowance() -> None:
    # 16,000 chars ≈ 4,000 tokens + 400 of schema overhead = 4,400 input tokens at $2/M, plus the
    # 1,800-token output allowance at $12/M.
    estimate = estimate_eval_cost(10, input_chars=16_000, model_ref="openai:gpt-5.6-terra")
    per_item = (4_400 / 1e6) * 2 + (EVAL_OUTPUT_TOKENS / 1e6) * 12
    assert EVAL_OUTPUT_TOKENS == 1_800
    assert estimate.model == "openai:gpt-5.6-terra"
    assert estimate.per_item_usd == pytest.approx(per_item, abs=1e-4)
    assert estimate.usd == pytest.approx(per_item * 10, abs=1e-4)


def test_is_linear_in_the_number_of_roles_and_zero_for_none() -> None:
    one = estimate_eval_cost(1, model_ref="openai:gpt-5.6-terra")
    fifteen = estimate_eval_cost(15, model_ref="openai:gpt-5.6-terra")
    assert one.usd is not None and fifteen.usd is not None
    assert fifteen.usd == pytest.approx(one.usd * 15, abs=1e-3)
    assert estimate_eval_cost(0, model_ref="openai:gpt-5.6-terra").usd == 0


def test_null_prices_for_a_model_with_no_price_on_file() -> None:
    estimate = estimate_eval_cost(3, model_ref="acme:mystery-1")
    assert (estimate.usd, estimate.per_item_usd, estimate.model) == (None, None, "acme:mystery-1")


def test_uses_the_rate_in_force_at_the_time() -> None:
    # A large batch, so rounding to a hundredth of a cent does not blur it.
    off_peak = estimate_eval_cost(
        1_000, model_ref="deepseek:deepseek-flash", at=datetime(2026, 9, 24, 18, tzinfo=UTC)
    )
    peak = estimate_eval_cost(
        1_000, model_ref="deepseek:deepseek-flash", at=datetime(2026, 9, 24, 10, tzinfo=UTC)
    )
    assert off_peak.usd is not None and peak.usd is not None
    assert off_peak.usd / peak.usd == pytest.approx(0.5, abs=1e-3)


def test_prices_specific_postings_from_the_real_prompt() -> None:
    instructions = len(eval_instructions(CANDIDATE))
    estimate = estimate_eval_cost_for(CANDIDATE, [1_000, 50_000], model_ref="openai:gpt-5.6-terra")
    average = (instructions + 400 + 1_000 + (instructions + 400 + MAX_DESCRIPTION_CHARS)) / 2
    assert estimate == estimate_eval_cost(2, input_chars=average, model_ref="openai:gpt-5.6-terra")
    # The configured evaluate model (the test settings' terra) is the default.
    assert estimate_eval_cost(1).model == "openai:gpt-5.6-terra"


def test_cost_phrase_reads_naturally_at_every_size() -> None:
    assert cost_phrase(None) is None
    assert cost_phrase(0.004) == "under a cent"
    assert cost_phrase(0.4) == "about $0.40"
    assert cost_phrase(1.234) == "about $1.23"
    assert cost_phrase(0.125) == "about $0.13"


# --- one pass through the model stack ------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
async def test_run_deep_eval_sends_both_halves_and_drops_invented_citations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str]] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        from apps.llm import fake

        seen.append((info.instructions or "", fake.latest_user_text(messages)))
        args = EVALUATION.model_dump(mode="json")
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, args)])

    fake_models(monkeypatch, {"evaluate": FunctionModel(model)})
    with llm_context(label="smoke"):
        result = await run_deep_eval(CANDIDATE, POSTING)

    assert result.score == 84 and result.recommendation == "apply"
    assert result.matched_requirements[0].fact_ids == ["work-1", "skill-1"]
    instructions, prompt = seen[0]
    assert "[work-1]" in instructions
    assert "Backend Engineer, Payments" in prompt
    rows = await db(
        lambda: list(
            LlmCall.objects.unscoped("test: the ledger")
            .filter(label="smoke")
            .values_list("purpose", "provider", "model")
        )
    )
    assert rows == [("evaluate", "fake", "evaluate")]
