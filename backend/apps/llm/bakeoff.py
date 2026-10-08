"""Bake-off fixtures: compare models for one purpose on fixed inputs (design §8.9).

A port of `src/lib/ranking/bakeoff-fixture.ts` and the `FIXTURES` registry of
`scripts/model-bakeoff.ts`. Which model should score five hundred postings is a question about
money and reliability, not taste, so it is answered with numbers: cost per run, latency, how
often a model's output fails the schema, and how far it drifts from a reference model.

Unlike TS, bake-off calls are ledgered (invariant 10 covers every call): `llm_bakeoff` runs
them under `label="bakeoff"`, with no user. The model is built for the explicit ref through the
usual stack (`registry.build_stack`), so a bake-off can compare several models in one process.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from django.db.models.functions import Length

from apps.core.db import db
from apps.jobs.models import Job
from apps.llm import registry
from apps.llm.budget import BudgetExceeded
from apps.llm.structured import structured_result
from apps.llm.usage import TokenCounts, tokens_from_usage
from apps.matching.schemas import FastScore
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
MAX_DESCRIPTION_CHARS = 4000


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


@dataclass(frozen=True, slots=True)
class Fixture:
    # Runs the purpose's prompt against one model ref, at one reasoning effort (None: the
    # purpose's own).
    run: Callable[[str, Effort | None], Awaitable[FixtureResult]]
    # 0-1: how closely a candidate's outputs track the reference's.
    agreement: Callable[[Sequence[Any], Sequence[Any]], float]


def scoring_sample(limit: int = SAMPLE_SIZE) -> list[Posting]:
    """A stable sample of real postings, ordered by id so every model sees the same set (a
    bake-off over different postings would measure the postings, not the models). Sync."""
    rows = (
        Job.objects.annotate(description_length=Length("description_text"))
        .filter(description_text__isnull=False, description_length__range=(1500, 12000))
        .order_by("id")
        .values_list("id", "title", "company_name", "locations", "remote", "description_text")[
            :limit
        ]
    )
    return [Posting(*row) for row in rows]


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
        result.outputs.append(ScoredFixture(posting.id, answer.output.score))
        result.tokens = result.tokens + tokens_from_usage(answer.usage)
    result.latency_ms = round((time.perf_counter() - started) * 1000)
    return result


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


# Filled in per purpose as each prompt gets a fixture: extraction (single column, two column,
# LaTeX export) and tailoring (job/resume pairs scored by the verifier) are still open.
FIXTURES: dict[Purpose, Fixture] = {
    "score": Fixture(run=run_scoring_fixture, agreement=score_agreement),
}
