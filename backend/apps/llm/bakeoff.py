"""Bake-off fixtures: compare models for one purpose on fixed inputs (design §8.9).

A port of `src/lib/ranking/bakeoff-fixture.ts` and the `FIXTURES` registry of
`scripts/model-bakeoff.ts`. Which model should score five hundred postings is a question about
money and reliability, not taste, so it is answered with numbers: cost per run, per-call latency
(median and 90th percentile), how often a model's output fails the schema, and how far it drifts
from a reference model.

Unlike TS, bake-off calls are ledgered (invariant 10 covers every call): `llm_bakeoff` runs
them under `label="bakeoff"`, with no user. The model is built for the explicit ref through the
usual stack (`registry.build_stack`), so a bake-off can compare several models in one process.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from django.db.models import F, Func, Q, QuerySet, TextField, Value
from django.db.models.functions import Length

import apps.accounts
from apps.accounts.schemas import SAMPLE_PREFERENCES
from apps.core.db import db
from apps.jobs.models import Job, Remote
from apps.llm import registry
from apps.llm.budget import BudgetExceeded
from apps.llm.structured import structured_result
from apps.llm.usage import TokenCounts, tokens_from_usage
from apps.matching.deep_eval import (
    CALL_TIMEOUT_S,
    EvalCandidate,
    EvalPosting,
    eval_instructions,
    eval_prompt,
    sanitize_deep_eval,
)
from apps.matching.fast_score import MAX_DESCRIPTION_CHARS
from apps.matching.schemas import DeepEval, FastScore
from apps.resumes import schema
from apps.resumes.text import resume_to_text
from foray.env import Effort, Purpose

# The scoring fixture's fixed prompt: a simplified rubric and a hard-coded candidate, so the
# same request reaches every model.
RUBRIC = """You triage job postings for a candidate. Score 0-100, where 85+ is
squarely what they asked for, 55-69 is plausible, and below 30 is clearly wrong.
Be decisive: a middling score for everything is useless triage.
fit_reasons: at most three short concrete phrases."""

CANDIDATE = """<candidate>
Senior backend engineer, six years of experience in Node.js, TypeScript,
Python and PostgreSQL. Has owned a billing service end to end and led a
migration off a monolith. Wants senior backend roles in the San Francisco Bay
Area or remote, minimum $190k base. US citizen. Dealbreakers: on-call more than
one week a month, full return-to-office.
</candidate>"""

SAMPLE_SIZE = 20
# Most of the sample is roles the candidate could plausibly want: a model is chosen on how it
# ranks the postings near the shortlist, and every model agrees that a sales role is a 5.
RELEVANT_SHARE = 0.6
# The titles both fixtures treat as relevant to their (backend engineer) candidates.
ENGINEERING_TITLES = r"(software|backend|platform|infrastructure|data|full[- ]?stack) engineer"


@dataclass(frozen=True, slots=True)
class Posting:
    id: str
    title: str
    company: str
    locations: list[str]
    remote: str
    description: str | None


@dataclass(frozen=True, slots=True)
class ScoredFixture:
    job_id: str
    score: int


@dataclass(slots=True)
class FixtureResult:
    outputs: list[Any] = field(default_factory=list)
    tokens: TokenCounts = field(default_factory=TokenCounts)
    latency_ms: int = 0
    schema_failures: int = 0
    call_ms: list[int] = field(default_factory=list)  # every call, failures included


def percentile(values: Sequence[int], fraction: float) -> int:
    """Nearest-rank percentile: the value at or below which `fraction` of them fall."""
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def no_checks(outputs: Sequence[Any], reference: Sequence[Any] | None) -> str:
    return "—"


@dataclass(frozen=True, slots=True)
class Fixture:
    # Runs the purpose's prompt against one model ref, at one reasoning effort (None: the
    # purpose's own).
    run: Callable[[str, Effort | None], Awaitable[FixtureResult]]
    # 0-1: how closely a candidate's outputs track the reference's.
    agreement: Callable[[Sequence[Any], Sequence[Any]], float]
    # A short note on quality the fixture can check without a reference, and on the drift from
    # it when there is one.
    checks: Callable[[Sequence[Any], Sequence[Any] | None], str] = no_checks


def spread_jobs() -> QuerySet[Job]:
    """Postings of a usable length in a stable order that spreads across companies: every model
    sees the same set (a bake-off over different postings would measure the postings, not the
    models), and ordered by id a sample would be one company's board."""
    return (
        Job.objects.annotate(
            description_length=Length("description_text"),
            spread=Func(F("id"), function="MD5", output_field=TextField()),
        )
        .filter(description_text__isnull=False, description_length__range=(1500, 12000))
        .order_by("spread", "id")
    )


def scoring_sample(limit: int = SAMPLE_SIZE) -> list[Posting]:
    """A stable sample of real postings, mostly engineering roles (`RELEVANT_SHARE`). Sync."""
    fields = ("id", "title", "company_name", "locations", "remote", "description_text")
    relevant = list(
        spread_jobs()
        .filter(title__iregex=ENGINEERING_TITLES)
        .values_list(*fields)[: math.ceil(limit * RELEVANT_SHARE)]
    )
    rest = list(
        spread_jobs()
        .exclude(title__iregex=ENGINEERING_TITLES)
        .values_list(*fields)[: limit - len(relevant)]
    )
    return [Posting(*row) for row in sorted([*relevant, *rest])]


def scoring_prompt(posting: Posting) -> str:
    description = (posting.description or "")[:MAX_DESCRIPTION_CHARS]
    return (
        "<posting>\n"
        f"Title: {posting.title}\n"
        f"Company: {posting.company}\n"
        f"Location: {' | '.join(posting.locations) or 'unstated'} ({posting.remote})\n"
        "\n"
        f"{description}\n"
        "</posting>"
    )


async def run_scoring_fixture(
    model_ref: str, effort: Effort | None = None, *, limit: int = SAMPLE_SIZE
) -> FixtureResult:
    """Scores the sample with one model, sequentially. A failure is counted, not raised: a
    model that fails a fifth of the time is exactly what this is meant to reveal. The budget
    guard's refusal is the exception: it says nothing about the model, so it ends the run
    (`llm_bakeoff` exits 1 with its sentence, design §8.6)."""
    sample = await db(scoring_sample, limit)
    stack = registry.build_stack("score", model_ref, effort=effort)
    result = FixtureResult()
    started = time.perf_counter()
    for posting in sample:
        call_started = time.perf_counter()
        try:
            answer = await structured_result(
                "score",
                FastScore,
                instructions=f"{RUBRIC}\n\n{CANDIDATE}",
                prompt=scoring_prompt(posting),
                model=stack,
            )
        except BudgetExceeded:
            raise
        except Exception:
            result.schema_failures += 1
            continue
        finally:
            result.call_ms.append(round((time.perf_counter() - call_started) * 1000))
        result.outputs.append(ScoredFixture(posting.id, answer.output.score))
        result.tokens = result.tokens + tokens_from_usage(answer.usage)
    result.latency_ms = round((time.perf_counter() - started) * 1000)
    return result


def rank_correlation(a: Sequence[float], b: Sequence[float]) -> float:
    """Spearman's rho, ties ranked by their average: whether two models put the postings in the
    same order, which is what picks the shortlist."""

    def ranks(values: Sequence[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        start = 0
        while start < len(order):
            end = start
            while end + 1 < len(order) and values[order[end + 1]] == values[order[start]]:
                end += 1
            for k in range(start, end + 1):
                out[order[k]] = (start + end) / 2
            start = end + 1
        return out

    ra, rb = ranks(a), ranks(b)
    mean_a, mean_b = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(ra, rb, strict=True))
    spread = math.sqrt(sum((x - mean_a) ** 2 for x in ra) * sum((y - mean_b) ** 2 for y in rb))
    return cov / spread if spread else 0.0


def score_band(score: int) -> str:
    """The rubric's triage bands, as the search acts on them."""
    return "good" if score >= 70 else "plausible" if score >= 55 else "wrong"


def score_checks(
    outputs: Sequence[ScoredFixture], reference: Sequence[ScoredFixture] | None
) -> str:
    """Against the reference: how many postings land in the same band, and the rank order."""
    if reference is None:
        return "—"
    by_id = {item.job_id: item.score for item in reference}
    pairs = [(by_id[item.job_id], item.score) for item in outputs if item.job_id in by_id]
    if len(pairs) < 2:
        return "—"
    same = sum(score_band(a) == score_band(b) for a, b in pairs)
    rho = rank_correlation([a for a, _ in pairs], [b for _, b in pairs])
    return f"bands {same}/{len(pairs)}, rank {rho:.2f}"


def score_agreement(
    candidate: Sequence[ScoredFixture], reference: Sequence[ScoredFixture]
) -> float:
    """Mean absolute score difference over the postings both scored, rescaled to 0-1: two models
    that agree within a few points on every posting are interchangeable for triage."""
    by_id = {item.job_id: item.score for item in reference}
    diffs = [abs(item.score - by_id[item.job_id]) for item in candidate if item.job_id in by_id]
    if not diffs:
        return 0.0
    return max(0.0, 1 - (sum(diffs) / len(diffs)) / 100)


# --- evaluate ------------------------------------------------------------------------------------

# The evaluation fixture reads full postings against the sample candidate that
# `create_test_user --with-resume` uses: a fixed person, so every model and every run sees the
# same resume, and nobody's real one leaves the building for a bake-off.
SAMPLE_RESUME_PATH = Path(apps.accounts.__file__).resolve().parent / "sample_resume.json"
# Only roles the sample candidate (a mid-level backend engineer in Oakland, open to hybrid) could
# plausibly want: an evaluation runs on postings the fast pass ranked highly, and agreeing that a
# staff role in Zurich is a skip says nothing about a model.
EVAL_SAMPLE_SIZE = 8
OFF_LEVEL_TITLES = (
    r"(staff|principal|lead|manager|director|intern|new grad|head of|distinguished|architect)"
)
BAY_AREA = (
    r"(san francisco|bay area|oakland|berkeley|emeryville|palo alto|mountain view|san jose"
    r"|sunnyvale|menlo park|redwood city|san mateo)"
)


@dataclass(frozen=True, slots=True)
class EvaluatedFixture:
    job_id: str
    score: int
    recommendation: str
    requirements: int  # matched requirements
    cited: int  # of those, the ones left with a real resume id after the citation check
    evaluation: dict[str, Any]  # the whole answer, for reading side by side (`--out`)


def sample_candidate() -> EvalCandidate:
    resume = schema.Resume.read(json.loads(SAMPLE_RESUME_PATH.read_text()))
    if resume is None:  # pragma: no cover - the sample is checked in
        raise RuntimeError(f"{SAMPLE_RESUME_PATH} is not a readable resume")
    return EvalCandidate(
        resume_id="sample",
        resume_version=1,
        resume_text=resume_to_text(resume),
        preferences=SAMPLE_PREFERENCES,
        focus=None,
        fact_ids=tuple(schema.fact_ids(resume)),
    )


def evaluation_sample(limit: int = EVAL_SAMPLE_SIZE) -> list[tuple[str, EvalPosting]]:
    """Engineering postings at a plausible level, in the Bay Area or remote, in `spread_jobs`
    order. Sync."""
    jobs = (
        spread_jobs()
        .annotate(
            places=Func(
                F("locations"), Value(" "), function="array_to_string", output_field=TextField()
            )
        )
        .filter(title__iregex=ENGINEERING_TITLES)
        .exclude(title__iregex=OFF_LEVEL_TITLES)
        .filter(Q(remote=Remote.REMOTE) | Q(places__iregex=BAY_AREA))[:limit]
    )
    return [
        (
            job.id,
            EvalPosting(
                title=job.title,
                company=job.company_name,
                locations=list(job.locations),
                remote=job.remote,
                employment_type=job.employment_type,
                salary_min=job.salary_min,
                salary_max=job.salary_max,
                salary_currency=job.salary_currency,
                salary_period=job.salary_period,
                posted_at=job.posted_at,
                description=job.description_text,
            ),
        )
        for job in jobs
    ]


async def run_evaluation_fixture(
    model_ref: str, effort: Effort | None = None, *, limit: int = EVAL_SAMPLE_SIZE
) -> FixtureResult:
    """Evaluates the sample with one model, sequentially, as `run_scoring_fixture` scores."""
    candidate = sample_candidate()
    sample = await db(evaluation_sample, limit)
    # Midnight today, so "posted N days ago" reads the same for every model in a run.
    now = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    stack = registry.build_stack("evaluate", model_ref, effort=effort)
    result = FixtureResult()
    started = time.perf_counter()
    for job_id, posting in sample:
        call_started = time.perf_counter()
        try:
            answer = await structured_result(
                "evaluate",
                DeepEval,
                instructions=eval_instructions(candidate),
                prompt=eval_prompt(posting, now),
                timeout=CALL_TIMEOUT_S,
                model=stack,
            )
        except BudgetExceeded:
            raise
        except Exception:
            result.schema_failures += 1
            continue
        finally:
            result.call_ms.append(round((time.perf_counter() - call_started) * 1000))
        value = sanitize_deep_eval(answer.output, candidate.fact_ids)
        result.outputs.append(
            EvaluatedFixture(
                job_id=job_id,
                score=value.score,
                recommendation=value.recommendation,
                requirements=len(value.matched_requirements),
                cited=sum(1 for item in value.matched_requirements if item.fact_ids),
                evaluation=value.dump(),
            )
        )
        result.tokens = result.tokens + tokens_from_usage(answer.usage)
    result.latency_ms = round((time.perf_counter() - started) * 1000)
    return result


def recommendation_agreement(
    candidate: Sequence[EvaluatedFixture], reference: Sequence[EvaluatedFixture]
) -> float:
    """The share of postings both evaluated where they reached the same recommendation: the
    verdict is what decides whether someone applies, so it is what has to agree."""
    by_id = {item.job_id: item.recommendation for item in reference}
    same = [item.recommendation == by_id[item.job_id] for item in candidate if item.job_id in by_id]
    return sum(same) / len(same) if same else 0.0


def evaluation_checks(
    outputs: Sequence[EvaluatedFixture], reference: Sequence[EvaluatedFixture] | None
) -> str:
    """How many matched requirements cite a real resume item (a model that invents ids, or none,
    is not showing its working), and the mean score difference from the reference."""
    requirements = sum(item.requirements for item in outputs)
    cited = sum(item.cited for item in outputs)
    note = f"cited {round(cited / requirements * 100) if requirements else 0}%"
    if reference is not None:
        by_id = {item.job_id: item.score for item in reference}
        diffs = [abs(item.score - by_id[item.job_id]) for item in outputs if item.job_id in by_id]
        if diffs:
            note += f", Δscore {round(sum(diffs) / len(diffs))}"
    return note


# Filled in per purpose as each prompt gets a fixture: extraction (single column, two column,
# LaTeX export) and tailoring (job/resume pairs scored by the verifier) are still open.
FIXTURES: dict[Purpose, Fixture] = {
    "score": Fixture(run=run_scoring_fixture, agreement=score_agreement, checks=score_checks),
    "evaluate": Fixture(
        run=run_evaluation_fixture, agreement=recommendation_agreement, checks=evaluation_checks
    ),
}
