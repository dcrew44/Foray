"""`manage.py llm_smoke` and `llm_bakeoff` (design §8.9), with mock vendors and fakes."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from io import StringIO
from typing import Any

import pytest
from django.core.management import CommandError, call_command
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from apps.llm import registry
from apps.llm.bakeoff import (
    RUBRIC,
    EvaluatedFixture,
    ScoredFixture,
    evaluation_checks,
    evaluation_sample,
    percentile,
    rank_correlation,
    recommendation_agreement,
    sample_candidate,
    score_agreement,
    score_checks,
    scoring_prompt,
    scoring_sample,
)
from apps.llm.management.commands.llm_bakeoff import (
    COLUMNS,
    console_table,
    js_round,
    split_spec,
    to_fixed,
    valid_spec,
)
from apps.llm.models import LlmCall
from foray.env import PURPOSES
from tests.factories import make_job, make_llm_call
from tests.llm.vendors import Vendor, patch_base_models

pytestmark = pytest.mark.django_db(transaction=True)


def run(*args: Any) -> tuple[int, str, str]:
    out, err = StringIO(), StringIO()
    try:
        call_command(*args, stdout=out, stderr=err)
        code = 0
    except SystemExit as exc:
        code = int(exc.code or 0)
    return code, out.getvalue(), err.getvalue()


def ledger() -> list[dict[str, Any]]:
    return list(
        LlmCall.objects.unscoped("tests: read the ledger")
        .order_by("created_at")
        .values("purpose", "provider", "model", "user_id", "label", "cost_usd", "error")
    )


# --- llm_smoke ------------------------------------------------------------------------------------


def test_smoke_calls_every_purpose(monkeypatch: pytest.MonkeyPatch) -> None:
    refs = {registry.model_ref(p) for p in PURPOSES}
    patch_base_models(monkeypatch, {ref: Vendor(ref) for ref in refs})
    code, out, _ = run("llm_smoke")
    lines = out.splitlines()
    assert code == 0
    assert lines[0] == "providers with credentials: openai, google, anthropic, deepseek"
    checks = [line for line in lines if line.startswith("✓")]
    assert len(checks) == 7
    assert checks[0].startswith("✓ chat      openai:gpt-5.6-luna              ")
    assert "ms  in=1000 cached=600 out=300  → " in checks[0]
    assert checks[0].endswith('→ {"city":"San Francisco","confident":true}')
    assert "all configured purposes answered. Check the ledger:" in out
    rows = ledger()
    assert [r["purpose"] for r in rows] == list(PURPOSES)
    assert {(r["label"], r["user_id"], r["error"]) for r in rows} == {("smoke", None, None)}
    assert rows[0]["cost_usd"] == Decimal("0.000452")
    assert rows[3]["model"] == "gpt-5.6-terra"  # evaluate


def test_smoke_filters_purposes(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_base_models(monkeypatch, {"openai:gpt-5.6-luna": Vendor("openai:gpt-5.6-luna")})
    code, out, _ = run("llm_smoke", "score", "verify")
    assert code == 0
    assert [line.split()[1] for line in out.splitlines() if line.startswith("✓")] == [
        "score",
        "verify",
    ]


def test_smoke_refuses_an_unknown_purpose() -> None:
    with pytest.raises(CommandError, match="Not an LLM purpose: summarise"):
        call_command("llm_smoke", "summarise", stdout=StringIO())


def test_smoke_under_llm_fake(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    env(llm_fake=True)
    monkeypatch.setattr(registry, "_MODELS", {})
    code, out, _ = run("llm_smoke")
    assert code == 0
    assert sum(line.startswith("✓") for line in out.splitlines()) == 7
    assert "(unpriced" not in out
    rows = ledger()
    assert len(rows) == 7
    assert {(r["provider"], r["label"], r["cost_usd"]) for r in rows} == {
        ("fake", "fake", Decimal(0))
    }


def test_smoke_fake_failure_exits_1(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    env(llm_fake=True, llm_fake_fail=("score",))
    monkeypatch.setattr(registry, "_MODELS", {})
    code, out, _ = run("llm_smoke", "score")
    assert code == 1
    assert "✗ score     fake:score: Fake failure" in out
    assert "1 purpose(s) failed." in out


def test_smoke_over_budget_exits_1_and_writes_nothing(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_llm_call(None, cost_usd=Decimal("0.000452"))
    env(llm_monthly_budget_usd=Decimal("0.000001"))
    vendor = Vendor("openai:gpt-5.6-luna")
    patch_base_models(monkeypatch, {"openai:gpt-5.6-luna": vendor})
    code, out, _ = run("llm_smoke", "chat")
    assert code == 1
    assert (
        "✗ chat      openai:gpt-5.6-luna: This month's model budget of $0.000001 is spent. "
        "Raise LLM_MONTHLY_BUDGET_USD to continue."
    ) in out
    assert vendor.bodies == []
    assert len(ledger()) == 1  # only the row written above


# --- llm_bakeoff ----------------------------------------------------------------------------------


def score_by_hash(offset: int):
    def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompt = next(
            p.content
            for m in messages
            for p in getattr(m, "parts", [])
            if isinstance(p, UserPromptPart)
        )
        score = (int(hashlib.sha1(str(prompt).encode()).hexdigest(), 16) + offset) % 101
        args = {
            "score": score,
            "fit_reasons": ["fits"],
            "red_flags": [],
            "seniority": "senior",
            "hard_requirement_misses": [],
        }
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, json.dumps(args))])

    return answer


def jobs_for_the_sample() -> None:
    for index in range(3):
        make_job(title=f"Backend engineer {index}", description_text="Python and Postgres. " * 100)
    make_job(title="Too short", description_text="short")


def test_bakeoff_prints_the_table_and_ledgers_its_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    jobs_for_the_sample()
    models = {
        "openai:gpt-5.6-luna": FunctionModel(score_by_hash(0)),
        "deepseek:deepseek-flash": FunctionModel(score_by_hash(1)),
        "openai:gpt-5.6-terra": FunctionModel(score_by_hash(0)),
    }
    monkeypatch.setattr(registry, "base_model", lambda ref: models[ref])
    code, out, err = run(
        "llm_bakeoff",
        "--purpose",
        "score",
        "--models",
        "openai:gpt-5.6-luna,deepseek:deepseek-flash",
        "--reference",
        "openai:gpt-5.6-terra",
        "--reasoning",
        "none",
    )
    assert (code, err) == (0, "")
    lines = out.splitlines()
    assert lines[0].startswith("┌─────────┬")
    assert [cell.strip() for cell in lines[1].split("│")[1:-1]] == [
        "(index)",
        "model",
        "cost",
        "per500",
        "p50",
        "p90",
        "reasoningShare",
        "schemaFail",
        "agreement",
        "checks",
    ]
    luna = [cell.strip() for cell in lines[3].split("│")[1:-1]]
    flash = [cell.strip() for cell in lines[4].split("│")[1:-1]]
    assert luna[1] == "'openai:gpt-5.6-luna'" and luna[7] == "'0/3'" and luna[8] == "'1.00'"
    assert flash[1] == "'deepseek:deepseek-flash'" and flash[8] != "'1.00'"
    assert luna[4].endswith("s'") and luna[9].startswith("'bands ")
    rows = ledger()
    assert len(rows) == 9  # reference, then two models, three postings each
    assert {(r["label"], r["user_id"]) for r in rows} == {("bakeoff", None)}
    assert [r["model"] for r in rows[:3]] == ["gpt-5.6-terra"] * 3


def test_bakeoff_over_budget_exits_1(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # A budget refusal is not the model's schema failing (§8.6): the bake-off stops, prints the
    # guard's sentence and exits 1, without calling the provider or writing a row.
    make_llm_call(None, cost_usd=Decimal("1"))
    env(llm_monthly_budget_usd=Decimal("0.5"))
    jobs_for_the_sample()
    calls: list[int] = []
    scorer = score_by_hash(0)

    def counting(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls.append(1)
        return scorer(messages, info)

    model = FunctionModel(counting)
    monkeypatch.setattr(registry, "base_model", lambda ref: model)
    code, out, err = run("llm_bakeoff", "--purpose", "score", "--models", "openai:gpt-5.6-luna")
    assert code == 1
    assert err.strip() == (
        "This month's model budget of $0.5 is spent. Raise LLM_MONTHLY_BUDGET_USD to continue."
    )
    assert out == ""  # no table blaming the model's schema
    assert calls == []
    assert len(ledger()) == 1  # only the row written above


def test_bakeoff_usage_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    assert run("llm_bakeoff", "--purpose", "score")[0] == 2
    code, _, err = run("llm_bakeoff", "--purpose", "score", "--models", "gpt-5")
    assert (code, err.strip()) == (2, 'Not a "provider:model" reference: gpt-5.')
    code, _, err = run("llm_bakeoff", "--purpose", "score", "--models", "openai:gpt-6-luna@max")
    assert (code, err.strip()) == (2, 'Not a "provider:model" reference: openai:gpt-6-luna@max.')
    code, _, err = run("llm_bakeoff", "--purpose", "tailor", "--models", "openai:gpt-5.6-luna")
    assert code == 1 and 'No fixtures for "tailor" yet.' in err
    code, _, err = run("llm_bakeoff", "--purpose", "score", "--models", "acme:model-1")
    assert code == 1 and 'names the unknown provider "acme"' in err
    assert "warning: no price for acme:model-1" in err


def test_bakeoff_under_llm_fake_runs_only_fakes(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    env(llm_fake=True)
    code, _, err = run("llm_bakeoff", "--purpose", "score", "--models", "openai:gpt-5.6-luna")
    assert code == 1 and "LLM_FAKE is on" in err and "openai:gpt-5.6-luna" in err
    assert ledger() == []

    jobs_for_the_sample()
    code, out, err = run(
        "llm_bakeoff", "--purpose", "score", "--models", "fake:score", "--reference", "fake:score"
    )
    assert (code, err) == (0, "")
    cells = [cell.strip() for cell in out.splitlines()[3].split("│")[1:-1]]
    assert cells[1:3] == ["'fake:score'", "'$0.0000'"] and cells[7:9] == ["'0/3'", "'1.00'"]
    rows = ledger()
    assert len(rows) == 6
    assert {(r["provider"], r["label"], r["cost_usd"]) for r in rows} == {
        ("fake", "fake", Decimal(0))
    }


def engineering_jobs() -> None:
    for index in range(3):
        make_job(title=f"Backend Engineer {index}", description_text="Python and Postgres. " * 100)
    make_job(title="Account Executive", description_text="Quota and pipeline. " * 100)
    make_job(title="Staff Backend Engineer", description_text="Python. " * 300)  # off level
    make_job(
        title="Backend Engineer", locations=["Zurich"], description_text="Go and Kubernetes. " * 100
    )
    make_job(
        title="Backend Engineer",
        locations=["Austin, TX"],
        remote="remote",
        description_text="Go and Kubernetes. " * 100,
    )


def test_evaluation_sample_reads_engineering_postings_in_full() -> None:
    engineering_jobs()
    sample = evaluation_sample()
    assert len(sample) == 4 and all("Engineer" in posting.title for _, posting in sample)
    assert {p.locations[0] for _, p in sample} == {"San Francisco, CA", "Austin, TX"}  # or remote
    assert [job_id for job_id, _ in sample] == [job_id for job_id, _ in evaluation_sample()]
    candidate = sample_candidate()
    # Every item the resume text shows carries its id, which is what an evaluation cites.
    assert candidate.fact_ids and any(f"[{f}]" in candidate.resume_text for f in candidate.fact_ids)


def test_bakeoff_evaluates_with_fakes_and_writes_its_outputs(env: Any, tmp_path: Any) -> None:
    env(llm_fake=True)
    engineering_jobs()
    out_file = tmp_path / "evaluate.json"
    code, out, err = run(
        "llm_bakeoff",
        "--purpose",
        "evaluate",
        "--models",
        "fake:evaluate",
        "--reference",
        "fake:evaluate",
        "--out",
        str(out_file),
    )
    assert (code, err) == (0, "")
    cells = [cell.strip() for cell in out.splitlines()[3].split("│")[1:-1]]
    assert cells[1] == "'fake:evaluate'" and cells[7:9] == ["'0/4'", "'1.00'"]
    assert cells[9].startswith("'cited ") and cells[9].endswith(", Δscore 0'")
    written = json.loads(out_file.read_text())
    assert list(written) == ["reference fake:evaluate", "fake:evaluate"]
    assert {"job_id", "score", "recommendation", "cited", "evaluation"} <= set(
        written["fake:evaluate"][0]
    )
    assert len(ledger()) == 8


def test_evaluation_agreement_is_the_shared_verdict() -> None:
    def item(job_id: str, score: int, recommendation: str, cited: int = 1) -> EvaluatedFixture:
        return EvaluatedFixture(job_id, score, recommendation, 2, cited, {})

    reference = [item("j1", 90, "strong"), item("j2", 60, "maybe")]
    candidate = [item("j1", 80, "apply", cited=2), item("j2", 64, "maybe", cited=0)]
    assert recommendation_agreement(candidate, reference) == 0.5
    assert recommendation_agreement(candidate, []) == 0
    assert evaluation_checks(candidate, reference) == "cited 50%, Δscore 7"
    assert evaluation_checks(candidate, None) == "cited 50%"


def test_a_model_may_carry_its_own_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    assert split_spec("deepseek:deepseek-flash@low") == ("deepseek:deepseek-flash", "low")
    assert split_spec("openai:gpt-6-luna") == ("openai:gpt-6-luna", None)
    assert valid_spec("openai:gpt-6-luna@none") and not valid_spec("openai:gpt-6-luna@max")
    jobs_for_the_sample()
    efforts: list[str] = []
    real = registry.build_stack

    def build_stack(purpose: Any, ref: str, *, effort: Any = None, **rest: Any) -> Any:
        efforts.append(f"{ref}@{effort}")
        return real(purpose, ref, effort=effort, **rest)

    monkeypatch.setattr(registry, "build_stack", build_stack)
    monkeypatch.setattr(registry, "base_model", lambda ref: FunctionModel(score_by_hash(0)))
    code, out, err = run(
        "llm_bakeoff",
        "--purpose",
        "score",
        "--models",
        "openai:gpt-6-luna@low,openai:gpt-6-luna",
        "--reasoning",
        "none",
    )
    assert (code, err) == (0, "")
    assert sorted(efforts) == ["openai:gpt-6-luna@low", "openai:gpt-6-luna@none"]
    assert "'openai:gpt-6-luna@low'" in out and "'$0.0" in out  # priced as gpt-6-luna


def test_scoring_sample_is_mostly_engineering_roles() -> None:
    for index in range(6):
        make_job(title=f"Backend Engineer {index}", description_text="Python. " * 300)
        make_job(title=f"Account Executive {index}", description_text="Quota. " * 300)
    sample = scoring_sample(limit=5)
    titles = [p.title for p in sample]
    assert sum("Engineer" in t for t in titles) == 3 and len(titles) == 5


def test_score_checks_compare_bands_and_order() -> None:
    reference = [ScoredFixture("j1", 90), ScoredFixture("j2", 60), ScoredFixture("j3", 10)]
    same_order = [ScoredFixture("j1", 72), ScoredFixture("j2", 50), ScoredFixture("j3", 5)]
    assert score_checks(same_order, reference) == "bands 2/3, rank 1.00"
    assert score_checks(same_order, None) == "—"
    assert rank_correlation([1, 2, 3], [3, 2, 1]) == pytest.approx(-1)
    assert rank_correlation([1, 1, 1], [1, 2, 3]) == 0


def test_percentile_is_nearest_rank() -> None:
    assert percentile([], 0.5) == 0
    assert percentile([300, 100, 200], 0.5) == 200
    assert percentile(list(range(1, 11)), 0.9) == 9
    assert percentile([5], 0.9) == 5


def test_scoring_sample_and_prompt() -> None:
    jobs_for_the_sample()
    sample = scoring_sample()
    assert [p.id for p in sample] == sorted(p.id for p in sample)  # a stable order
    assert len(sample) == 3 and all(p.title != "Too short" for p in sample)
    assert [p.id for p in scoring_sample()] == [p.id for p in sample]
    prompt = scoring_prompt(sample[0])
    assert prompt.startswith(f"<posting>\nTitle: {sample[0].title}\nCompany: {sample[0].company}\n")
    assert "Location: San Francisco, CA (hybrid)\n\nPython and Postgres." in prompt
    assert prompt.endswith("\n</posting>")
    assert RUBRIC.startswith("You triage job postings for a candidate.")


def test_agreement_and_formatting() -> None:
    a = [ScoredFixture("j1", 80), ScoredFixture("j2", 40)]
    assert score_agreement(a, a) == 1
    assert score_agreement(a, [ScoredFixture("j1", 70), ScoredFixture("j3", 10)]) == pytest.approx(
        0.9
    )
    assert score_agreement(a, []) == 0
    assert (
        to_fixed(0.00045, 4) == "0.0004"  # each value as Node prints it
        and to_fixed(1.005, 2) == "1.00"
        and to_fixed(0.125, 2) == "0.13"
        and to_fixed(12.35, 1) == "12.3"
        and to_fixed(0, 2) == "0.00"
    )
    assert (js_round(2.5), js_round(-2.5), js_round(92.4)) == (3, -2, 92)
    table = console_table([dict.fromkeys(COLUMNS, "x")])
    assert table.splitlines()[3] == (
        "│ 0       │ 'x'   │ 'x'  │ 'x'    │ 'x' │ 'x' │ 'x'            │ 'x'        │ 'x'       "
        "│ 'x'    │"
    )
