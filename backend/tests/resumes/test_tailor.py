"""The tailor's mapping, notes and prompt (design §11.4; port of `tailor.test.ts`, 12 cases).

The tailor's model output is untrusted. Mapping it back onto the base is where an invented item
first gets stopped: only base ids survive, basics are the base's, and skills are base skills with
the base's grouping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from apps.resumes import tailor
from apps.resumes.schema import Resume, TailorChange, Verification
from apps.resumes.tailor import (
    EvaluationNotes,
    TailorModelOutput,
    build_tailor_prompt,
    evaluation_notes,
    map_tailored_output,
    save_tailored_resume,
    tailor_resume,
)
from tests.factories import make_match, make_user, sample_resume_data


def base() -> Resume:
    return Resume.model_validate(
        {
            "basics": {
                "name": "Dana Reyes",
                "email": "dana@example.com",
                "links": [{"id": "l1", "url": "github.com/dana", "label": "github.com/dana"}],
            },
            "summary": "Backend engineer.",
            "work": [
                {
                    "id": "w1",
                    "source": "user",
                    "company": "Kestrel",
                    "position": "Engineer II",
                    "startDate": "2023",
                    "endDate": "Present",
                    "highlights": ["Rebuilt ingestion."],
                },
                {
                    "id": "w2",
                    "company": "Northgate",
                    "position": "Engineer I",
                    "startDate": "2021",
                    "endDate": "2023",
                    "highlights": ["Built the reporting API."],
                },
            ],
            "education": [
                {"id": "e1", "institution": "Bellwater", "highlights": ["Minor in Statistics."]}
            ],
            "projects": [
                {"id": "p1", "name": "Tideline", "description": "A CLI."},
                {"id": "p2", "name": "Slowhand", "description": "A profiler."},
            ],
            "skills": [
                {"id": "s1", "name": "TypeScript", "category": "Languages"},
                {"id": "s2", "name": "PostgreSQL", "category": "Data"},
            ],
        }
    )


def output(**overrides: Any) -> TailorModelOutput:
    data: dict[str, Any] = {
        "summary": "Backend engineer who rebuilt ingestion.",
        "work": [
            {
                "id": "w2",
                "company": "Northgate",
                "position": "Engineer I",
                "location": None,
                "startDate": "2021",
                "endDate": "2023",
                "summary": None,
                "highlights": ["Built the reporting API.", "  "],
            },
            {
                "id": "w1",
                "company": "Kestrel",
                "position": "Engineer II",
                "location": None,
                "startDate": "2023",
                "endDate": "Present",
                "summary": None,
                "highlights": ["Rebuilt the ingestion service."],
            },
        ],
        "education": [
            {
                "id": "e1",
                "institution": "Bellwater",
                "area": None,
                "studyType": None,
                "startDate": None,
                "endDate": None,
                "score": None,
                "highlights": ["Minor in Statistics."],
            }
        ],
        "projects": [
            {
                "id": "p2",
                "name": "Slowhand",
                "description": "A profiler.",
                "url": None,
                "startDate": None,
                "endDate": None,
                "highlights": [],
            },
            {
                "id": "p1",
                "name": "Tideline",
                "description": "A CLI.",
                "url": None,
                "startDate": None,
                "endDate": None,
                "highlights": [],
            },
        ],
        "skills": ["postgresql", "TypeScript"],
        "changes": [
            {
                "section": "work",
                "item_id": "w1",
                "before": "Rebuilt ingestion.",
                "after": "Rebuilt the ingestion service.",
                "reason": "Clearer",
                "evidence_fact_ids": ["w1"],
            }
        ],
    }
    data.update(overrides)
    return TailorModelOutput.model_validate(data)


# --- map_tailored_output -------------------------------------------------------------------------


def test_drops_an_item_whose_id_is_not_a_base_id_and_reports_it():
    invented = {
        "id": "w-invented",
        "company": "Stripe",
        "position": "Staff Engineer",
        "location": None,
        "startDate": "2024",
        "endDate": "Present",
        "summary": None,
        "highlights": ["Scaled payments."],
    }
    mapped = map_tailored_output(
        base(), output(work=[*(item.dump() for item in output().work), invented])
    )
    assert [item.id for item in mapped.resume.work] == ["w1", "w2"]
    assert [(drop.section, drop.item_id, drop.text) for drop in mapped.dropped] == [
        ("work", "w-invented", "Staff Engineer — Stripe")
    ]


def test_roles_keep_base_order_but_projects_keep_the_tailors():
    mapped = map_tailored_output(base(), output())
    assert [item.id for item in mapped.resume.work] == ["w1", "w2"]
    assert [item.id for item in mapped.resume.projects] == ["p2", "p1"]


def test_copies_provenance_from_the_base_and_drops_blank_bullets():
    mapped = map_tailored_output(base(), output())
    assert mapped.resume.work[0].source == "user"
    assert mapped.resume.work[1].source == "resume"
    assert mapped.resume.work[1].highlights == ["Built the reporting API."]


def test_copies_basics_from_the_base_verbatim():
    original = base()
    assert map_tailored_output(original, output()).resume.basics == original.basics


def test_maps_skills_to_base_skills_by_name_with_the_base_id_and_category():
    mapped = map_tailored_output(
        base(), output(skills=["postgresql", "TypeScript", "Kubernetes", "TYPESCRIPT"])
    )
    assert [skill.dump() for skill in mapped.resume.skills] == [
        {"id": "s2", "source": "resume", "name": "PostgreSQL", "category": "Data"},
        {"id": "s1", "source": "resume", "name": "TypeScript", "category": "Languages"},
    ]
    assert [(drop.section, drop.text) for drop in mapped.dropped] == [("skills", "Kubernetes")]


def test_ignores_a_repeated_id_rather_than_duplicating_the_item():
    first = output().work[1].dump()
    mapped = map_tailored_output(base(), output(work=[first, {**first, "highlights": ["Again."]}]))
    assert len(mapped.resume.work) == 1
    assert mapped.resume.work[0].highlights == ["Rebuilt the ingestion service."]


def test_replaces_an_impossible_date_with_the_base_value():
    work = [{**item.dump(), "startDate": "x" * 60} for item in output().work]
    mapped = map_tailored_output(base(), output(work=work))
    assert mapped.resume.work[0].start_date == "2023"


def test_passes_the_change_log_through_trimmed():
    assert map_tailored_output(base(), output()).changes == [
        TailorChange(
            section="work",
            item_id="w1",
            before="Rebuilt ingestion.",
            after="Rebuilt the ingestion service.",
            reason="Clearer",
            evidence_fact_ids=["w1"],
        )
    ]


# --- evaluation_notes ----------------------------------------------------------------------------


def test_reads_requirements_talking_points_and_gaps():
    notes = evaluation_notes(
        {
            "matched_requirements": [
                {
                    "requirement": "Postgres at scale",
                    "evidence": "40M-row table",
                    "fact_ids": ["w1"],
                    "strength": "strong",
                }
            ],
            "gaps": [{"gap": "Kubernetes", "severity": "learnable", "note": "not on resume"}],
            "talking_points": ["Latency work"],
        },
        {"hard_requirement_misses": ["Security clearance"]},
    )
    assert notes == EvaluationNotes(
        matched=["Postgres at scale — shown by: 40M-row table"],
        talking_points=["Latency work"],
        gaps=["Kubernetes (not on resume)", "Security clearance"],
    )


def test_tolerates_a_missing_or_malformed_evaluation():
    empty = EvaluationNotes(matched=[], talking_points=[], gaps=[])
    assert evaluation_notes(None, None) == empty
    assert evaluation_notes("nonsense", [1, 2]) == empty


# --- build_tailor_prompt -------------------------------------------------------------------------


@dataclass
class Job:
    title: str = "Backend Engineer"
    company_name: str = "Acme"
    locations: list[str] = field(default_factory=lambda: ["Remote"])
    remote: str = "remote"
    description_text: str | None = "We use Postgres. " * 2000
    description_raw: str | None = None


def test_stable_facts_in_the_instructions_and_the_posting_in_the_prompt():
    instructions, prompt = build_tailor_prompt(
        base(), Job(), EvaluationNotes(matched=[], talking_points=[], gaps=["Kubernetes"]), None
    )
    assert "[w1] Engineer II — Kestrel" in instructions
    assert "<exact_fields>" in instructions
    assert '"Minor in Statistics."' in instructions  # education highlights the prose omits
    assert "Title: Backend Engineer" in prompt
    assert "never claim or imply them:\n- Kubernetes" in prompt


def test_caps_the_posting():
    _, prompt = build_tailor_prompt(base(), Job())
    assert len(prompt) < 11_000


# --- the call ------------------------------------------------------------------------------------


async def test_tailor_resume_asks_the_tailor_purpose_and_maps_the_answer(
    monkeypatch: pytest.MonkeyPatch,
):
    seen: dict[str, Any] = {}

    async def fake_structured(purpose: str, output_type: type, **kwargs: Any) -> Any:
        seen.update(purpose=purpose, output_type=output_type, **kwargs)
        return output()

    monkeypatch.setattr(tailor, "structured", fake_structured)
    mapped = await tailor_resume(base(), Job(description_text="Go and Postgres."))
    assert seen["purpose"] == "tailor"
    assert seen["output_type"] is TailorModelOutput
    assert (seen["instructions"], seen["prompt"]) == build_tailor_prompt(
        base(), Job(description_text="Go and Postgres."), evaluation_notes(None, None)
    )
    assert [item.id for item in mapped.resume.work] == ["w1", "w2"]


# --- saving --------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_save_tailored_resume_versions_per_match_and_points_the_match_at_it():
    user = make_user(with_resume=True)
    match = make_match(user)
    tailored = Resume.model_validate(sample_resume_data())
    verification = Verification(
        passed=True,
        checked_at="2026-10-08T12:00:00.000Z",
        unsupported=[],
        revised=False,
        checked=3,
        model_checked=0,
        revisions=[],
        model_error=None,
        base_resume_id=None,
        base_version=1,
    )
    change = TailorChange(
        section="summary",
        item_id=None,
        before="Old.",
        after="New.",
        reason="Clearer",
        evidence_fact_ids=[],
    )

    first = save_tailored_resume(
        user.pk, match.id, resume=tailored, changes=[change], verification=verification
    )
    second = save_tailored_resume(
        user.pk, match.id, resume=tailored, changes=[], verification=verification
    )
    assert (first.kind, first.version, second.version) == ("tailored", 1, 2)
    assert first.match_id == match.id
    assert first.changes == [change.dump()]
    assert first.verification == verification.dump()
    assert "checkedAt" in verification.dump()  # stored keys as today
    match.refresh_from_db()
    assert match.tailored_resume_id == second.id
