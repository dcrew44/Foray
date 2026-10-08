"""The resume, as stored in `resumes.data`, and its audit trail (design §2.3, §11.3;
`map/llm.md` §16, `map/database.md` §3.3; a port of `src/lib/resume/schema.ts`).

- `Resume` is JSON-Resume-like, with provenance: every item carries `id` and `source`. A later
  import must never overwrite something the user typed (`user` always wins a merge), and
  tailoring cites the id of the base fact each changed line rests on.
- Dates stay as the resume wrote them ("2024-06", "June 2024", "Present"), 1 to 40 characters,
  never normalised.
- `ExtractedResume` is what the extraction model returns: written out separately because every
  property must be required (absence is null) under OpenAI's strict structured output, and ids
  and sources are ours to assign, never the model's.
- `TailorChange` (`resumes.changes`) and `Verification` (`resumes.verification`) keep the keys
  they have today: snake_case in a change and a claim, camelCase at the top of a verification.

The models carry no docstrings: pydantic would turn them into JSON-schema descriptions, and
`ExtractedResume` is sent to the model as it was from TypeScript.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints

from apps.core.choices import CAMEL, JsonModel

ItemSource = Literal["resume", "linkedin_pdf", "user"]

DateString = Annotated[str, StringConstraints(min_length=1, max_length=40)]


class _Item(JsonModel):
    model_config = CAMEL

    id: str  # stable within a resume; regenerated only when an item is created
    source: ItemSource = "resume"


class Link(_Item):
    label: str
    url: str


class Basics(JsonModel):
    model_config = CAMEL

    name: str = ""
    headline: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    links: list[Link] = Field(default_factory=list)


class WorkItem(_Item):
    company: str
    position: str
    location: str | None = None
    start_date: DateString | None = None
    end_date: DateString | None = None
    summary: str | None = None
    highlights: list[str] = Field(default_factory=list)  # one bullet per achievement, verbatim


class EducationItem(_Item):
    institution: str
    area: str | None = None
    study_type: str | None = None
    start_date: DateString | None = None
    end_date: DateString | None = None
    score: str | None = None
    highlights: list[str] = Field(default_factory=list)


class ProjectItem(_Item):
    name: str
    description: str | None = None
    url: str | None = None
    start_date: DateString | None = None
    end_date: DateString | None = None
    highlights: list[str] = Field(default_factory=list)


class SkillItem(_Item):
    name: str
    category: str | None = None  # the resume's own grouping ("Languages"), never inferred


class Resume(JsonModel):
    model_config = CAMEL

    basics: Basics = Field(default_factory=Basics)
    summary: str | None = None
    work: list[WorkItem] = Field(default_factory=list)
    education: list[EducationItem] = Field(default_factory=list)
    projects: list[ProjectItem] = Field(default_factory=list)
    skills: list[SkillItem] = Field(default_factory=list)


# --- What the extraction model returns -----------------------------------------------------------


class _Extracted(JsonModel):
    model_config = CAMEL


class ExtractedLink(_Extracted):
    label: str
    url: str


class ExtractedBasics(_Extracted):
    name: str
    headline: str | None
    email: str | None
    phone: str | None
    location: str | None
    links: list[ExtractedLink]


class ExtractedWork(_Extracted):
    company: str
    position: str
    location: str | None
    start_date: DateString | None
    end_date: DateString | None
    summary: str | None
    highlights: list[str]


class ExtractedEducation(_Extracted):
    institution: str
    area: str | None
    study_type: str | None
    start_date: DateString | None
    end_date: DateString | None
    score: str | None
    highlights: list[str]


class ExtractedProject(_Extracted):
    name: str
    description: str | None
    url: str | None
    start_date: DateString | None
    end_date: DateString | None
    highlights: list[str]


class ExtractedSkill(_Extracted):
    name: str
    category: str | None


class ExtractedResume(_Extracted):
    basics: ExtractedBasics
    summary: str | None
    work: list[ExtractedWork]
    education: list[ExtractedEducation]
    projects: list[ExtractedProject]
    skills: list[ExtractedSkill]


def _new_fact_id() -> str:
    return str(uuid.uuid4())


def with_ids(
    extracted: ExtractedResume,
    source: ItemSource,
    new_id: Callable[[], str] = _new_fact_id,
) -> Resume:
    """Assigns ids and provenance to a freshly extracted resume (`withIds`)."""

    def stamp(item: _Extracted) -> dict[str, Any]:
        return {**item.dump(), "id": new_id(), "source": source}

    basics = extracted.basics.dump()
    basics["links"] = [stamp(link) for link in extracted.basics.links]
    return Resume.model_validate(
        {
            "basics": basics,
            "summary": extracted.summary,
            "work": [stamp(item) for item in extracted.work],
            "education": [stamp(item) for item in extracted.education],
            "projects": [stamp(item) for item in extracted.projects],
            "skills": [stamp(item) for item in extracted.skills],
        }
    )


def fact_ids(resume: Resume) -> list[str]:
    """Every factual item's id, flattened: what the tailoring verifier checks against."""
    items: list[_Item] = [
        *resume.basics.links,
        *resume.work,
        *resume.education,
        *resume.projects,
        *resume.skills,
    ]
    return [item.id for item in items]


# --- Tailoring's audit trail ---------------------------------------------------------------------

TailorSection = Literal["summary", "work", "projects", "education", "skills"]
TAILOR_SECTIONS: tuple[TailorSection, ...] = ("summary", "work", "projects", "education", "skills")


class TailorChange(JsonModel):
    # One edit, as stored in `resumes.changes`.
    section: TailorSection
    item_id: str | None  # the base item the edit belongs to; null for the summary and skills
    before: str | None  # the base text, or null when nothing was replaced
    after: str | None  # the tailored text, or null when something was dropped
    reason: str
    evidence_fact_ids: list[str]  # base item ids the new text rests on


class UnsupportedClaim(JsonModel):
    section: str
    item_id: str | None
    text: str
    reason: str
    source: Literal["rule", "model"]


class Revision(UnsupportedClaim):
    action: Literal["replaced", "dropped", "reverted"]
    replacement: str | None  # the base text or value put back, when something was


class Verification(JsonModel):
    model_config = CAMEL

    passed: bool  # nothing on the final resume is unsupported
    checked_at: str  # ISO 8601
    unsupported: list[UnsupportedClaim]  # still unsupported after revision
    revised: bool
    checked: int  # lines checked
    model_checked: int  # rewritten lines the model judged
    revisions: list[Revision]
    model_error: str | None  # set when the model check could not run
    base_resume_id: str | None
    base_version: int | None
