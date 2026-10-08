"""The `extract` (resumes), `tailor` and `verify` fakes (design §8.9), run through a real
pydantic-ai agent over a `FunctionModel`, and the extraction flow on them."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from apps.resumes import extract, fakes, tailor, verify
from apps.resumes.extract import ResumeReadError, extract_resume
from apps.resumes.fakes import parse_resume_text
from apps.resumes.schema import ExtractedResume, Resume
from apps.resumes.tailor import tailor_resume
from apps.resumes.verify import Claim, model_judge, verify_tailored
from tests.factories import sample_resume_data
from tests.resumes.support import fixture_bytes, golden, structured_with


class Listing(BaseModel):
    postings: list[str]


@dataclass
class Job:
    title: str
    company_name: str
    locations: list[str] = field(default_factory=list)
    remote: str = "unknown"
    description_text: str | None = None
    description_raw: str | None = None


@pytest.mark.parametrize("override", [fakes.extract_resume, fakes.tailor, fakes.verify])
async def test_each_fake_declines_another_output_type(override: Any):
    seen: list[Any] = []

    def probe(messages: Any, info: Any) -> Any:
        seen.append(override(messages, info))
        return ModelResponse(
            parts=[ToolCallPart(tool_name=info.output_tools[0].name, args={"postings": []})]
        )

    agent = Agent(FunctionModel(probe), output_type=Listing, instructions="x")
    await agent.run("Read this careers page.")
    assert seen == [None]


def test_extract_fake_reads_the_fixtures_by_their_headings():
    single = parse_resume_text(golden("resumes/pdf/single-column.json")["text"])
    assert single.basics.name == "Dana Whitlock Reyes"
    assert single.basics.email == "dana.w.reyes@example.com"
    assert single.basics.phone == "(555) 0134-2087"
    assert single.basics.location == "Sacramento, CA"
    assert [link.url for link in single.basics.links] == [
        "github.com/dwreyes-dev",
        "linkedin.com/in/danawreyes",
        "danareyes.example.dev",
    ]
    assert [(w.position, w.company, w.start_date, w.end_date) for w in single.work] == [
        ("Software Engineer II", "Kestrel Logistics Group", "March 2023", "Present"),
        ("Software Engineer I", "Northgate Analytics", "July 2021", "February 2023"),
        ("Software Engineering Intern", "Willowmere Civic Tech", "May 2020", "August 2020"),
    ]
    assert len(single.work[0].highlights) == 4  # wrapped lines joined back on
    assert [(e.study_type, e.area, e.institution) for e in single.education] == [
        ("B.S.", "Computer Science", "Bellwater State University")
    ]
    assert [p.name for p in single.projects] == ["Tideline", "Slowhand"]
    assert single.skills[0].name == "TypeScript" and single.skills[0].category == "Languages"
    assert "AWS (ECS, RDS, S3, Lambda)" in [skill.name for skill in single.skills]
    assert single.summary is not None and single.summary.startswith("Backend-leaning")

    docx = parse_resume_text(golden("resumes/docx/plain.json")["text"])
    assert (len(docx.work), len(docx.education)) == (3, 1)
    assert docx.work[0].company == "Marrow & Finch Software"

    serif = parse_resume_text(golden("resumes/pdf/serif-dense.json")["text"])
    assert serif.education and serif.skills  # letter-spaced headings still found


async def test_extract_resume_on_the_fake(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(extract, "structured", structured_with(fakes.extract_resume))
    result = await extract_resume(fixture_bytes("resumes/single-column.pdf"), "pdf")
    assert result.used_vision is False
    assert result.page_count == 2
    assert result.text == golden("resumes/pdf/single-column.json")["text"]
    assert len(result.resume.work) == 3
    assert all(item.source == "resume" and item.id for item in result.resume.work)
    assert len({item.id for item in result.resume.skills}) == len(result.resume.skills)

    vision = await extract_resume(
        fixture_bytes("resumes/single-column.pdf"), "pdf", force_vision=True
    )
    assert vision.used_vision is True
    assert vision.resume.basics.name == "Resume from page images"

    docx = await extract_resume(fixture_bytes("resumes/plain.docx"), "docx", source="user")
    assert docx.used_vision is False
    assert docx.resume.work[0].source == "user"


async def test_a_garbled_pdf_takes_the_vision_path(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(extract, "structured", structured_with(fakes.extract_resume))
    parsed = await extract.read_document(fixture_bytes("resumes/single-column.pdf"), "pdf")
    garbled = extract.ParsedDocument(
        text="", page_count=2, garble=extract.GarbleReport(garbled=True, reasons=["scan"])
    )
    assert parsed.garble.garbled is False
    result = await extract.extract_from(garbled, fixture_bytes("resumes/single-column.pdf"), "pdf")
    assert result.used_vision is True
    # DOCX has no vision path, garbled or not.
    docx = await extract.extract_from(garbled, fixture_bytes("resumes/plain.docx"), "docx")
    assert docx.used_vision is False


async def test_unreadable_documents_raise_plain_words():
    with pytest.raises(ResumeReadError, match=r"^Invalid PDF structure\.$"):
        await extract.read_document(b"%PDF-1.4 garbage", "pdf")
    with pytest.raises(ResumeReadError, match=r"^That Word file could not be read\.$"):
        await extract.read_document(b"PK\x03\x04 not a zip", "docx")


async def test_tailor_fake_rewrites_nothing_and_verification_passes(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(tailor, "structured", structured_with(fakes.tailor))
    monkeypatch.setattr(verify, "structured", structured_with(fakes.verify))

    job = Job(
        title="Backend Engineer",
        company_name="Stripe",
        locations=["Remote"],
        remote="remote",
        description_text="We need Python and PostgreSQL.",
    )
    base = Resume.model_validate(sample_resume_data())
    draft = await tailor_resume(base, job)
    assert draft.dropped == []
    assert draft.changes == []
    assert draft.resume.dump() == base.dump()

    result = await verify_tailored(base, draft.resume, draft.changes)
    assert result.findings == []
    assert result.model_checked == 0  # nothing was rewritten, so the judge was not asked


async def test_verify_fake_marks_every_claim_supported(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(verify, "structured", structured_with(fakes.verify))
    base = Resume.model_validate(sample_resume_data())
    claims = [
        Claim(
            key=f"work:{base.work[0].id}:highlight:{index}",
            section="work",
            item_id=base.work[0].id,
            field="highlight",
            index=index,
            text=f"Rewritten line {index}.",
            verbatim=False,
        )
        for index in range(3)
    ]
    verdicts = await model_judge(base, claims, [])
    assert set(verdicts) == {claim.key for claim in claims}
    assert all(verdict.supported and verdict.reason is None for verdict in verdicts.values())


def test_register_hands_every_override_to_the_fake_registry(monkeypatch: pytest.MonkeyPatch):
    registered: list[tuple[str, Any]] = []
    stub = types.ModuleType("apps.llm.fake")
    stub.register = lambda purpose, fn: registered.append((purpose, fn))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "apps.llm.fake", stub)
    fakes.register()
    assert registered == [
        ("extract", fakes.extract_resume),
        ("tailor", fakes.tailor),
        ("verify", fakes.verify),
    ]


def test_the_extract_fake_output_is_a_valid_extracted_resume():
    data = parse_resume_text("Ada\n\nEXPERIENCE\nEngineer 2020 – 2021\nAcme — Remote\nBuilt it.")
    assert ExtractedResume.model_validate(data.dump()) == data
