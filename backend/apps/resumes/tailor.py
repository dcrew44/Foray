"""Tailoring: the base resume, edited for one posting (design §11.4; a verbatim port of
`src/lib/resume/tailor.ts`).

The model is an editor here, never an author. It may choose, reorder and reword what the base
resume already says; it may not add a fact. That rule is stated plainly in the instructions, but
instructions are not a guarantee: `verify.py` checks the output against the base facts and
reverts anything unsupported before a tailored resume is saved.

This module produces the draft (the prompt, pinned by the §8.10 goldens; the model call; the
mapping of the model's output back onto base items by id) and saves the verified result: the
tailored version and `job_matches.tailored_resume` in one transaction.
"""

from __future__ import annotations

import importlib
import re
import unicodedata
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from django.db import IntegrityError, transaction
from django.db.models import Max
from django.db.models.functions import Now

from apps.accounts.schemas import Preferences
from apps.core.choices import CAMEL, JsonModel
from apps.core.jsonio import js_json_indent
from apps.llm.structured import structured
from apps.matching.models import JobMatch
from apps.resumes.models import Resume as ResumeRow
from apps.resumes.models import ResumeKind
from apps.resumes.schema import (
    Resume,
    Revision,
    TailorChange,
    TailorSection,
    Verification,
)
from apps.resumes.text import JS_SPACE, js_trim, resume_to_text

__all__ = [
    "INSTRUCTIONS",
    "MAX_POSTING_CHARS",
    "EvaluationNotes",
    "MappedTailoring",
    "MappingDrop",
    "Posting",
    "TailorModelOutput",
    "build_tailor_prompt",
    "evaluation_notes",
    "map_tailored_output",
    "posting_text",
    "save_tailored_resume",
    "skill_key",
    "tailor_resume",
]

# How much of a posting the tailor reads.
MAX_POSTING_CHARS = 10_000


# --- What the model returns ----------------------------------------------------------------------
#
# Same shape as the extraction schema, with two differences. Every item carries the `id` of the
# base item it came from, because a tailored item can only be an edited base item. And basics
# are absent: they are copied from the base verbatim, so asking the model to echo them would only
# create a chance to change a phone number. Skills are names only for the same reason: the
# grouping is the resume's own and is copied from the base. Every property is required, with
# absence as null (OpenAI strict mode). No docstrings: they would become schema descriptions.


class TailoredWork(JsonModel):
    model_config = CAMEL

    id: str
    company: str
    position: str
    location: str | None
    start_date: str | None
    end_date: str | None
    summary: str | None
    highlights: list[str]


class TailoredEducation(JsonModel):
    model_config = CAMEL

    id: str
    institution: str
    area: str | None
    study_type: str | None
    start_date: str | None
    end_date: str | None
    score: str | None
    highlights: list[str]


class TailoredProject(JsonModel):
    model_config = CAMEL

    id: str
    name: str
    description: str | None
    url: str | None
    start_date: str | None
    end_date: str | None
    highlights: list[str]


class TailorModelOutput(JsonModel):
    summary: str | None
    work: list[TailoredWork]
    education: list[TailoredEducation]
    projects: list[TailoredProject]
    skills: list[str]  # names of base skills, most relevant first
    changes: list[TailorChange]


INSTRUCTIONS = """You tailor a candidate's resume to one job posting. You are an editor, not an author: every statement on the tailored resume must already be true on the original resume below.

What you should do — a tailored resume that reads exactly like the original has not been tailored:
- Select. Keep the roles, projects, bullets and skills that matter most for this posting. Drop weak or irrelevant bullets rather than padding. Keep the most recent role and every education entry.
- Reorder bullets within each role, and reorder projects and skills, so the most relevant to this posting come first. Keep roles and education in their original order.
- Rephrase bullets so each leads with what this posting cares about — the outcome, the kind of system, or the technology it asks for — using the posting's vocabulary only where it means exactly the same thing as the original ("Postgres" for "PostgreSQL" is fine; "Kubernetes" for "Docker" is not; "APIs" for "services" is not). Rewording that only moves emphasis is exactly the job; a bullet that already fits well can stay as it is.
- Rewrite the summary for this posting, using only facts stated somewhere in the resume. Do not name the target company.

Every rewritten line is checked against the original item it sits under, and anything unsupported is reverted, so a rewrite that adds a fact gains nothing.

What you must never do:
- Add an employer, job title, date, degree, school, certification, skill, tool, technology, metric or number that is not in the original. That includes numbers computed from existing ones: "from 840 ms to 190 ms" must not become "77% faster" or "4x faster".
- Move a fact between items. A bullet may only use facts from the role, project or education entry it sits under.
- Claim more ownership, scope or seniority than the original shows: "contributed to" does not become "led", "helped build" does not become "architected".
- Claim anything listed under Gaps — those are things the candidate does not have.

Copy exactly, character for character, from the Exact fields block:
- the id of every item you keep;
- company, position, location, startDate and endDate for roles; institution, area, studyType, score, startDate and endDate for education; name, url, startDate and endDate for projects — null where the block has null;
- skill names, exactly as listed. Return a subset of them, most relevant to this posting first.
Keep an education entry's highlights and a role's summary as they are unless rewording one makes it more relevant; the same rules apply.

Length: the tailored resume should fit on one page. Roughly: at most 4–5 bullets for the most relevant role, 2–3 for older or less relevant roles, 1–3 for a project, and a summary of two or three sentences. Shorter and sharper beats complete.

changes: one entry per edit — each rewritten bullet (before = the original bullet, after = your version), each dropped bullet or item (after = null), the summary rewrite, and one entry describing the skills selection (section "skills", item_id null). reason says in a few words why the edit helps for this posting. evidence_fact_ids lists the [id]s of the original items the new text rests on. Do not log items you left untouched."""  # noqa: E501 (prompt text, pinned by the §8.10 goldens)


# --- What the evaluations say --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvaluationNotes:
    """What a deep evaluation (or, failing that, the fast score) says about this match."""

    matched: list[str]
    talking_points: list[str]
    gaps: list[str]


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str(value: Any) -> str:
    return js_trim(value) if isinstance(value, str) else ""


def _list(value: Any, read: Callable[[Any], str], limit: int = 10) -> list[str]:
    if not isinstance(value, list):
        return []
    items = [item for item in map(read, value) if item][:limit]
    return [f"{item[:297]}…" if len(item) > 300 else item for item in items]


def _matched(item: Any) -> str:
    if isinstance(item, str):
        return item
    record = _record(item)
    requirement, evidence = _str(record.get("requirement")), _str(record.get("evidence"))
    if requirement and evidence:
        return f"{requirement} — shown by: {evidence}"
    return requirement or evidence


def _gap(item: Any) -> str:
    if isinstance(item, str):
        return item
    record = _record(item)
    gap, note = _str(record.get("gap")), _str(record.get("note"))
    return f"{gap} ({note})" if gap and note else gap


def evaluation_notes(deep_eval: Any, fast_eval: Any) -> EvaluationNotes:
    """Reads the stored evaluations leniently: they are written by another pass, and a shape
    change there should cost the tailor its hints, not fail the run."""
    deep, fast = _record(deep_eval), _record(fast_eval)
    gaps = [
        *_list(deep.get("gaps"), _gap),
        *_list(fast.get("hard_requirement_misses"), _str),
    ]
    return EvaluationNotes(
        matched=_list(deep.get("matched_requirements"), _matched),
        talking_points=_list(deep.get("talking_points"), _str),
        gaps=list(dict.fromkeys(gaps))[:12],
    )


# --- The prompt ----------------------------------------------------------------------------------


class Posting(Protocol):
    """What the tailor reads of a posting (a `jobs.Job` has all of it)."""

    @property
    def title(self) -> str: ...
    @property
    def company_name(self) -> str: ...
    @property
    def locations(self) -> Sequence[str]: ...
    @property
    def remote(self) -> str: ...
    @property
    def description_text(self) -> str | None: ...
    @property
    def description_raw(self) -> str | None: ...


def _exact_fields(base: Resume) -> dict[str, Any]:
    """The exact values the model must copy. `resume_to_text` is written for reading and renders
    a missing date as "?" or "present"; a tailor copying from it would faithfully reproduce the
    placeholder. This block has the real values, plus the education highlights and project
    dates the prose leaves out."""
    return {
        "work": [
            {
                "id": item.id,
                "company": item.company,
                "position": item.position,
                "location": item.location,
                "startDate": item.start_date,
                "endDate": item.end_date,
            }
            for item in base.work
        ],
        "education": [
            {
                "id": item.id,
                "institution": item.institution,
                "area": item.area,
                "studyType": item.study_type,
                "score": item.score,
                "startDate": item.start_date,
                "endDate": item.end_date,
                "highlights": item.highlights,
            }
            for item in base.education
        ],
        "projects": [
            {
                "id": item.id,
                "name": item.name,
                "url": item.url,
                "startDate": item.start_date,
                "endDate": item.end_date,
            }
            for item in base.projects
        ],
        "skills": [skill.name for skill in base.skills],
    }


def _html_to_text(html: str) -> str:
    # Step 3C's cleaner (`jobs/sources/clean.py`), looked up when a posting has only HTML: the
    # resumes app does not import the sources package at module load.
    clean = importlib.import_module("apps.jobs.sources.clean")
    return clean.html_to_text(html)


def posting_text(job: Posting) -> str:
    description = js_trim(job.description_text or "") or (
        _html_to_text(job.description_raw) if job.description_raw else ""
    )
    return description[:MAX_POSTING_CHARS] or "(no description)"


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def build_tailor_prompt(
    base: Resume,
    job: Posting,
    notes: EvaluationNotes | None = None,
    preferences: Preferences | None = None,
) -> tuple[str, str]:
    """`(instructions, prompt)`: the instructions and the candidate's facts first, the posting
    last. The stable prefix is what a provider's prompt cache can reuse when the same user
    tailors for several jobs."""
    instructions = (
        f"{INSTRUCTIONS}\n\n<resume>\n{resume_to_text(base)}\n</resume>\n\n"
        f"<exact_fields>\n{js_json_indent(_exact_fields(base), 1)}\n</exact_fields>"
    )

    goals = [
        line
        for line in (
            f"Target roles: {', '.join(preferences.target_roles)}"
            if preferences is not None and preferences.target_roles
            else None,
            f"Seniority: {preferences.seniority}"
            if preferences is not None and preferences.seniority
            else None,
        )
        if line
    ]

    evaluation: list[str] = []
    if notes is not None:
        if notes.matched:
            evaluation.append(
                f"What the posting asks for that the candidate has:\n{_bullets(notes.matched)}"
            )
        if notes.talking_points:
            evaluation.append(f"Worth emphasising:\n{_bullets(notes.talking_points)}")
        if notes.gaps:
            evaluation.append(
                "Gaps — the candidate does NOT have these; never claim or imply them:\n"
                f"{_bullets(notes.gaps)}"
            )

    locations = " | ".join(job.locations) or "unstated"
    parts = [
        "Tailor the resume for this posting.",
        f"<posting>\nTitle: {job.title}\nCompany: {job.company_name}\n"
        f"Location: {locations} ({job.remote})\n\n{posting_text(job)}\n</posting>",
        "<evaluation>\n" + "\n\n".join(evaluation) + "\n</evaluation>" if evaluation else None,
        "<candidate_goals>\n" + "\n".join(goals) + "\n</candidate_goals>" if goals else None,
    ]
    return instructions, "\n\n".join(part for part in parts if part)


# --- Mapping the output onto the base ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MappingDrop:
    """Something the mapping refused to carry into the tailored resume."""

    section: TailorSection
    item_id: str | None
    text: str
    reason: str

    def as_revision(self) -> Revision:
        """How the run records it: a rule finding that was dropped."""
        return Revision(
            section=self.section,
            item_id=self.item_id,
            text=self.text,
            reason=self.reason,
            source="rule",
            action="dropped",
            replacement=None,
        )


@dataclass(frozen=True, slots=True)
class MappedTailoring:
    resume: Resume
    changes: list[TailorChange]
    dropped: list[MappingDrop]


_WS_RUN = re.compile(f"[{re.escape(JS_SPACE)}]+")


def skill_key(name: str) -> str:
    """Case- and whitespace-insensitive skill identity."""
    return js_trim(_WS_RUN.sub(" ", unicodedata.normalize("NFKC", name).lower()))


def _clean(values: list[str]) -> list[str]:
    return [trimmed for value in values if (trimmed := js_trim(value))]


def _nullable(value: str | None) -> str | None:
    return js_trim(value) if value is not None and js_trim(value) else None


def _date(value: str | None, original: str | None) -> str | None:
    """The storage schema caps dates at 40 characters. Anything longer cannot be a verbatim copy
    of a base date, so it is replaced by the base value here rather than failing the whole run
    at parse time."""
    trimmed = _nullable(value)
    return original if trimmed is not None and len(trimmed) > 40 else trimmed


_ITEM_NOUN = {"work": "a role", "education": "an education entry", "projects": "a project"}


def map_tailored_output(base: Resume, output: TailorModelOutput) -> MappedTailoring:
    """Maps the model's output onto the storage shape, anchored to the base.

    - Basics come from the base verbatim.
    - An item is only kept if its id is a base item's id; `source` is the base item's. An
      invented item is dropped here and reported.
    - Skills are matched to base skills by name; the id, provenance and category come from the
      base. An unknown skill is dropped and reported.
    - Roles and education keep the base order, whatever order the model used.

    Everything else (the fields the model was told to copy, the bullets it rewrote) is kept as
    the model wrote it, for the verifier to judge."""
    dropped: list[MappingDrop] = []

    def anchor[B: Any, T: Any](
        section: Literal["work", "education", "projects"],
        base_items: list[B],
        items: list[T],
        describe: Callable[[T], str],
        keep_base_order: bool,
    ) -> list[tuple[B, T]]:
        by_id = {item.id: (item, index) for index, item in enumerate(base_items)}
        seen: set[str] = set()
        kept: list[tuple[B, T, int]] = []
        for item in items:
            item_id = js_trim(item.id)
            match = by_id.get(item_id)
            if match is None:
                dropped.append(
                    MappingDrop(
                        section=section,
                        item_id=item_id or None,
                        text=describe(item),
                        reason=f"Not {_ITEM_NOUN[section]} on your resume, so it was removed.",
                    )
                )
                continue
            if item_id in seen:
                continue
            seen.add(item_id)
            kept.append((match[0], item, match[1]))
        if keep_base_order:
            kept.sort(key=lambda entry: entry[2])
        return [(original, item) for original, item, _ in kept]

    work = [
        {
            "id": original.id,
            "source": original.source,
            "company": js_trim(item.company),
            "position": js_trim(item.position),
            "location": _nullable(item.location),
            "startDate": _date(item.start_date, original.start_date),
            "endDate": _date(item.end_date, original.end_date),
            "summary": _nullable(item.summary),
            "highlights": _clean(item.highlights),
        }
        for original, item in anchor(
            "work",
            base.work,
            output.work,
            lambda item: " — ".join(part for part in (item.position, item.company) if part),
            True,
        )
    ]
    education = [
        {
            "id": original.id,
            "source": original.source,
            "institution": js_trim(item.institution),
            "area": _nullable(item.area),
            "studyType": _nullable(item.study_type),
            "startDate": _date(item.start_date, original.start_date),
            "endDate": _date(item.end_date, original.end_date),
            "score": _nullable(item.score),
            "highlights": _clean(item.highlights),
        }
        for original, item in anchor(
            "education", base.education, output.education, lambda item: item.institution, True
        )
    ]
    projects = [
        {
            "id": original.id,
            "source": original.source,
            "name": js_trim(item.name),
            "description": _nullable(item.description),
            "url": _nullable(item.url),
            "startDate": _date(item.start_date, original.start_date),
            "endDate": _date(item.end_date, original.end_date),
            "highlights": _clean(item.highlights),
        }
        for original, item in anchor(
            "projects", base.projects, output.projects, lambda item: item.name, False
        )
    ]

    skills_by_name = {skill_key(skill.name): skill for skill in base.skills}
    skill_seen: set[str] = set()
    skills: list[dict[str, Any]] = []
    for name in output.skills:
        key = skill_key(name)
        if not key or key in skill_seen:
            continue
        original_skill = skills_by_name.get(key)
        if original_skill is None:
            dropped.append(
                MappingDrop(
                    section="skills",
                    item_id=None,
                    text=js_trim(name),
                    reason=f"{js_trim(name)} is not a skill on your resume, so it was removed.",
                )
            )
            continue
        skill_seen.add(key)
        skills.append(original_skill.dump())

    resume = Resume.model_validate(
        {
            "basics": base.basics.dump(),
            "summary": _nullable(output.summary),
            "work": work,
            "education": education,
            "projects": projects,
            "skills": skills,
        }
    )
    changes = [
        TailorChange(
            section=change.section,
            item_id=_nullable(change.item_id),
            before=_nullable(change.before),
            after=_nullable(change.after),
            reason=js_trim(change.reason),
            evidence_fact_ids=_clean(change.evidence_fact_ids),
        )
        for change in output.changes
    ]
    return MappedTailoring(resume=resume, changes=changes, dropped=dropped)


async def tailor_resume(
    base: Resume,
    job: Posting,
    *,
    deep_eval: Any = None,
    fast_eval: Any = None,
    preferences: Preferences | None = None,
) -> MappedTailoring:
    """One `tailor` call: the draft, mapped onto the base. The run verifies it next."""
    instructions, prompt = build_tailor_prompt(
        base, job, evaluation_notes(deep_eval, fast_eval), preferences
    )
    output = await structured("tailor", TailorModelOutput, instructions=instructions, prompt=prompt)
    return map_tailored_output(base, output)


# --- Saving ---------------------------------------------------------------------------------------

_TAILORED_VERSION = "resumes_tailored_version"


def save_tailored_resume(
    user_id: str,
    match_id: uuid.UUID,
    *,
    resume: Resume,
    changes: list[TailorChange],
    verification: Verification,
) -> ResumeRow:
    """Saves the verified resume as the match's next tailored version and makes it the match's
    current one, in one transaction: a match never points at a resume that failed to save.
    Retried once when another writer took the same version number."""
    for attempt in (1, 2):
        try:
            with transaction.atomic():
                newest = (
                    ResumeRow.objects.for_user(user_id)
                    .filter(kind=ResumeKind.TAILORED, match_id=match_id)
                    .aggregate(newest=Max("version"))["newest"]
                )
                row = ResumeRow.objects.create_for(
                    user_id,
                    kind=ResumeKind.TAILORED,
                    match_id=match_id,
                    version=(newest or 0) + 1,
                    data=resume.dump(),
                    changes=[change.dump() for change in changes],
                    verification=verification.dump(),
                )
                JobMatch.objects.for_user(user_id).filter(id=match_id).update(
                    tailored_resume_id=row.id, updated_at=Now()
                )
                return row
        except IntegrityError as error:
            diag = getattr(error.__cause__, "diag", None)
            if attempt == 2 or getattr(diag, "constraint_name", None) != _TAILORED_VERSION:
                raise
    raise AssertionError("unreachable")  # pragma: no cover
