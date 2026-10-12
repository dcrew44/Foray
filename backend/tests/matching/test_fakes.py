"""The `score` and `evaluate` fakes (design §8.9) and a whole search on `LLM_FAKE=1`.

The last tests are step 5A's live check run in-process: with `LLM_FAKE` on, a search started
through `POST /api/searches` reaches gate 1, an approval through `POST
/api/approvals/{id}/decision` moves the run to `running`, gate 2 is approved, and the run
completes with shortlisted matches, for $0 (every ledger row `label='fake'`). The worker is a
real procrastinate Worker in this process (`run_deferred()`), not `manage.py worker`.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from django.conf import settings
from django.utils import timezone
from pydantic import BaseModel

from apps.accounts.schemas import Preferences
from apps.llm import fake, registry
from apps.llm.models import LlmCall
from apps.llm.structured import structured_result
from apps.matching import fakes as matching_fakes
from apps.matching.brief import EMPTY_BRIEF
from apps.matching.deep_eval import EvalCandidate, EvalPosting, eval_instructions, eval_prompt
from apps.matching.fast_score import RUBRIC, ScorePosting, posting_prompt
from apps.matching.models import JobMatch
from apps.matching.prefilter import SEARCH_INSTRUCTIONS, DerivedSearch, derive_search_prompt
from apps.matching.schemas import DeepEval, FastScore
from apps.resumes.schema import Resume, fact_ids
from apps.resumes.text import resume_to_text
from apps.runs.models import Approval, Event, Run
from tests.factories import make_company, make_job, sample_resume_data
from tests.queue import run_deferred


@pytest.fixture(autouse=True)
def overrides_registered() -> None:
    # `register` ignores a repeat, so this only matters if another test's fixture swapped the
    # override table while this module was first imported.
    fake.register("score", matching_fakes.derived_search_reply)
    fake.register("score", matching_fakes.fast_score_reply)
    fake.register("evaluate", matching_fakes.deep_eval_reply)


async def ask[T: BaseModel](purpose: str, output: type[T], instructions: str, prompt: str) -> T:
    result = await structured_result(
        purpose,  # pyright: ignore[reportArgumentType]
        output,
        instructions=instructions,
        prompt=prompt,
        model=fake.fake_model(purpose),
    )
    return result.output


def posting(title: str, company: str = "Acme") -> ScorePosting:
    return ScorePosting(
        id="greenhouse:acme:1",
        title=title,
        company=company,
        locations=["San Francisco, CA"],
        remote="hybrid",
        salary_min=None,
        salary_max=None,
        description="Build things.",
    )


async def test_derive_reads_the_roles_from_the_prompt() -> None:
    saved = Preferences(target_roles=["Backend Engineer", "Platform engineer"], seniority="mid")
    answer = await ask(
        "score", DerivedSearch, SEARCH_INSTRUCTIONS, derive_search_prompt(saved, None)
    )
    assert answer.title_include == ["backend engineer", "platform engineer"]
    assert answer.brief == EMPTY_BRIEF
    hints = EMPTY_BRIEF.model_copy(update={"roles": ["Data analyst"]})
    resolved = await ask(
        "score", DerivedSearch, SEARCH_INSTRUCTIONS, derive_search_prompt(saved, "x", hints)
    )
    assert resolved.title_include == ["data analyst"]
    empty = await ask(
        "score", DerivedSearch, SEARCH_INSTRUCTIONS, derive_search_prompt(Preferences(), None)
    )
    assert empty.title_include == ["software engineer"]


async def test_fast_scores_are_stable_per_posting_and_spread() -> None:
    scores = {}
    for title in ("Backend Engineer", "Data Engineer", "Software Engineer", "SRE", "Designer"):
        first = await ask("score", FastScore, RUBRIC, posting_prompt(posting(title)))
        again = await ask("score", FastScore, RUBRIC, posting_prompt(posting(title)))
        assert first == again
        assert 40 <= first.score <= 99
        assert first.fit_reasons == [f"Fake triage of {title}: no model was called."]
        scores[title] = first.score
    assert len(set(scores.values())) > 1


async def test_deep_reviews_cite_the_resume_and_agree_with_their_band() -> None:
    resume = Resume.model_validate(sample_resume_data())
    candidate = EvalCandidate(
        resume_id="r",
        resume_version=1,
        resume_text=resume_to_text(resume),
        preferences=Preferences(),
        focus=None,
        fact_ids=tuple(fact_ids(resume)),
    )
    review = await ask(
        "evaluate",
        DeepEval,
        eval_instructions(candidate),
        eval_prompt(
            EvalPosting(
                title="Backend Engineer",
                company="Acme",
                locations=[],
                remote="remote",
                employment_type=None,
                salary_min=None,
                salary_max=None,
                salary_currency=None,
                salary_period=None,
                posted_at=None,
                description=None,
            )
        ),
    )
    assert 50 <= review.score <= 99
    assert review.recommendation == matching_fakes.recommendation_for(review.score)
    # The resume text's first item is its first role.
    assert review.matched_requirements[0].fact_ids == [resume.work[0].id]
    assert "Backend Engineer at Acme" in review.summary


async def test_other_output_types_fall_through_to_the_schema_default() -> None:
    class Other(BaseModel):
        name: str

    assert (await ask("score", Other, "x", "y")).name == ""


# --- a whole search on fakes ---------------------------------------------------------------------


@pytest.fixture
def llm_fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "ENV", settings.ENV.model_copy(update={"llm_fake": True}))
    monkeypatch.setattr(registry, "_MODELS", {})
    yield


TITLES = (
    "Software Engineer, Payments",
    "Software Engineer, Platform",
    "Software Engineer, Data",
    "Software Engineer, Growth",
    "Backend Engineer, Billing",
    "Backend Engineer, Search",
    "Backend Engineer, Infra",
    "Backend Engineer, API",
)


@pytest.mark.django_db(transaction=True)
def test_a_search_on_fakes_reaches_both_gates_and_shortlists(
    llm_fake: None, signed_in: Any
) -> None:
    api, user = signed_in(with_resume=True)
    company = make_company(name="Fakeco", slug="fakeco", last_fetched_at=timezone.now())
    for title in TITLES:
        make_job(company=company, title=title)

    started = api.post("/api/searches", {})
    assert started.status_code == 202
    run = Run.objects.for_user(user.pk).get(id=started.json()["runId"])
    run_deferred()

    assert Run.objects.for_user(user.pk).get(id=run.id).status == "awaiting_approval"
    gate1 = Approval.objects.for_user(user.pk).get(run=run)
    assert gate1.kind == "deep_eval_batch"
    assert gate1.title.endswith("(under a cent)")
    decided = api.post(f"/api/approvals/{gate1.id}/decision", {"approved": True})
    assert decided.status_code == 200
    assert Run.objects.for_user(user.pk).get(id=run.id).status == "running"
    run_deferred()

    gate2 = Approval.objects.for_user(user.pk).filter(run=run, kind="shortlist_proposal").get()
    assert gate2.status == "pending"
    api.post(f"/api/approvals/{gate2.id}/decision", {"approved": True})
    run_deferred()

    done = Run.objects.for_user(user.pk).get(id=run.id)
    assert done.status == "completed"
    assert done.output is not None and done.output["shortlisted"] >= 1
    shortlisted = JobMatch.objects.for_user(user.pk).filter(stage="shortlisted").count()
    assert shortlisted == done.output["shortlisted"]
    last = Event.objects.for_user(user.pk).filter(run=run).order_by("-id").first()
    assert last is not None and last.kind == "run.completed"
    # $0: every call went to a fake, ledgered as such.
    calls = LlmCall.objects.unscoped("tests").filter(run_id=run.id)
    assert calls.count() >= 1 + len(TITLES)
    assert set(calls.values_list("label", "provider")) == {("fake", "fake")}
    assert sum(calls.values_list("cost_usd", flat=True)) == 0


@pytest.mark.django_db(transaction=True)
def test_a_failing_fake_fails_the_search_with_its_words(
    llm_fake: None, monkeypatch: pytest.MonkeyPatch, signed_in: Any
) -> None:
    monkeypatch.setattr(
        settings, "ENV", settings.ENV.model_copy(update={"llm_fake_fail": ("score",)})
    )
    api, user = signed_in(with_resume=True)
    run_id = api.post("/api/searches", {}).json()["runId"]
    run_deferred()
    failed = Run.objects.for_user(user.pk).get(id=run_id)
    assert (failed.status, failed.error) == ("failed", "Fake failure")
    kinds = list(
        Event.objects.for_user(user.pk)
        .filter(run_id=run_id)
        .order_by("id")
        .values_list("kind", flat=True)
    )
    assert kinds == ["run.started", "run.failed"]
