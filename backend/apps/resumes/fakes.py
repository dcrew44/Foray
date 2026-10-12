"""The `LLM_FAKE` overrides this lane owns (design §8.9): `extract` for resumes, `tailor` and
`verify`. Deterministic, free, and good enough for the upload, tailor and render flows to run
end to end on fakes.

Each override is a pydantic-ai `FunctionModel` function, `(messages, info) -> ModelResponse`,
answering through the output tool (`info.output_tools[0]`). It returns None when the request is
not one it knows (another lane's output type, such as the careers-page listing extractor that
step 3C registers for `extract` too), so the registry can try the next override or fall back to
the schema-driven default.

- `extract_resume` parses the `<resume>` text with line heuristics: name, contact details and
  links from the top, then sections by their headings, roles and schools by their date ranges,
  skills by `Category: a, b` lines. The vision path has no text and gets a near-empty resume.
- `tailor` rewrites nothing: it returns the base resume as the instructions' `<resume>` prose
  and `<exact_fields>` block give it, every skill, and no changes, so verification passes.
- `verify` marks every claim supported.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import re
from collections.abc import Callable
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo

from apps.resumes.schema import (
    ExtractedBasics,
    ExtractedEducation,
    ExtractedLink,
    ExtractedProject,
    ExtractedResume,
    ExtractedSkill,
    ExtractedWork,
)

__all__ = ["OVERRIDES", "extract_resume", "parse_resume_text", "register", "tailor", "verify"]

type Override = Callable[[list[ModelMessage], AgentInfo], ModelResponse | None]


# --- Plumbing -------------------------------------------------------------------------------------


def _prompt_text(messages: list[ModelMessage]) -> str:
    """The text of the latest user prompt."""
    for message in reversed(messages):
        if not isinstance(message, ModelRequest):
            continue
        texts: list[str] = []
        for part in message.parts:
            if isinstance(part, UserPromptPart):
                if isinstance(part.content, str):
                    texts.append(part.content)
                else:
                    texts.extend(item for item in part.content if isinstance(item, str))
        if texts:
            return "".join(texts)
    return ""


def _output_properties(info: AgentInfo) -> set[str]:
    if not info.output_tools:
        return set()
    return set(info.output_tools[0].parameters_json_schema.get("properties", {}))


def _answer(info: AgentInfo, args: dict[str, Any]) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=args)])


def _block(text: str, tag: str) -> str | None:
    match = re.search(rf"<{tag}>\n(.*?)\n</{tag}>", text, re.DOTALL)
    return match.group(1) if match else None


# --- extract (resumes) ----------------------------------------------------------------------------

_EXTRACTED_KEYS = {"basics", "summary", "work", "education", "projects", "skills"}
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"\+?\(?\d{3}\)?[\s.-]?\d{3,4}[\s.–-]?\d{4}")
_LINK = re.compile(
    r"\b(?:https?://)?(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}(?:/\S*)?", re.I
)
_LOCATION = re.compile(r"^[A-Z][\w .'-]+, [A-Z]{2}$")
_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
_DATE = rf"(?:{_MONTH}\s+)?\d{{4}}"
_DATE_RANGE = re.compile(
    rf"(?P<start>{_DATE})\s*[–—-]\s*(?P<end>Present|Current|Now|{_DATE})\s*$", re.I
)
_SINGLE_DATE = re.compile(rf"(?P<start>{_DATE})\s*$")
_SPLIT_ORG = re.compile(r"\s+[—–-]\s+")
_BULLET = re.compile(r"^[•●▪◦*-]\s*")
_HEADINGS = {
    "SUMMARY": "summary",
    "PROFILE": "summary",
    "ABOUT": "summary",
    "EXPERIENCE": "work",
    "WORK EXPERIENCE": "work",
    "PROFESSIONAL EXPERIENCE": "work",
    "EMPLOYMENT": "work",
    "EDUCATION": "education",
    "SKILLS": "skills",
    "TECHNICAL SKILLS": "skills",
    "PROJECTS": "projects",
    "SELECTED PROJECTS": "projects",
}


_HEADING_KEYS = {heading.replace(" ", ""): section for heading, section in _HEADINGS.items()}


def _heading(line: str) -> str | None:
    """The section a heading line starts. Spaces are ignored, so letter-spaced headings
    ("S U M M A R Y", "E D U C AT I O N") read as their words."""
    return _HEADING_KEYS.get(re.sub(r"\s+", "", line).upper())


def _split_list(text: str) -> list[str]:
    """Commas outside parentheses."""
    items, depth, current = [], 0, ""
    for char in text:
        depth += char == "("
        depth -= char == ")"
        if char == "," and depth == 0:
            items.append(current)
            current = ""
        else:
            current += char
    items.append(current)
    return [item.strip() for item in items if item.strip()]


def _dated(line: str) -> tuple[str, str | None, str | None] | None:
    """`(title, start, end)` for a line ending in a date range or a year."""
    match = _DATE_RANGE.search(line) or _SINGLE_DATE.search(line)
    if not match or match.start() == 0:
        return None
    groups = match.groupdict()
    return line[: match.start()].strip(), groups.get("start"), groups.get("end")


def _org(line: str) -> tuple[str, str | None]:
    parts = _SPLIT_ORG.split(line, maxsplit=1)
    return parts[0].strip(), (parts[1].strip() if len(parts) > 1 else None)


def _paragraphs(lines: list[str]) -> list[str]:
    """Bullet lines, with wrapped continuation lines (starting lowercase) joined back on."""
    out: list[str] = []
    for line in lines:
        line = _BULLET.sub("", line)
        if out and line[:1].islower():
            out[-1] = f"{out[-1]} {line}"
        else:
            out.append(line)
    return out


def parse_resume_text(text: str) -> ExtractedResume:
    """A deterministic, good-enough reading of resume text (never used with a real model)."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    sections: dict[str, list[str]] = {"header": []}
    current = "header"
    for line in lines:
        section = _heading(line)
        if section is not None:
            current = section
            sections.setdefault(current, [])
        else:
            sections.setdefault(current, []).append(line)

    header = sections["header"]
    header_text = "\n".join(header)
    email = _EMAIL.search(header_text)
    phone = _PHONE.search(header_text)
    location = next(
        (
            part.strip()
            for line in header[1:]
            for part in re.split(r"\s*[|·•]\s*", line)
            if _LOCATION.match(part.strip())
        ),
        None,
    )
    links = [
        ExtractedLink(label=link, url=link)
        for line in header[1:]
        for link in _LINK.findall(_EMAIL.sub(" ", line))
        if "/" in link or link.count(".") >= 2
    ]

    def entries(section: str) -> list[list[str]]:
        groups: list[list[str]] = []
        for line in sections.get(section, []):
            if _dated(line) is not None:
                groups.append([line])
            elif groups:
                groups[-1].append(line)
        return groups

    work: list[ExtractedWork] = []
    for group in entries("work"):
        title, start, end = _dated(group[0]) or (group[0], None, None)
        company, place = _org(group[1]) if len(group) > 1 else ("", None)
        work.append(
            ExtractedWork(
                company=company,
                position=title,
                location=place,
                start_date=start,
                end_date=end,
                summary=None,
                highlights=_paragraphs(group[2:]),
            )
        )

    education: list[ExtractedEducation] = []
    for group in entries("education"):
        title, start, end = _dated(group[0]) or (group[0], None, None)
        degree, _, area = title.partition(" ")
        institution, _ = _org(group[1]) if len(group) > 1 else ("", None)
        education.append(
            ExtractedEducation(
                institution=institution,
                area=area or None,
                study_type=degree or None,
                start_date=start,
                end_date=end,
                score=None,
                highlights=_paragraphs(group[2:]),
            )
        )

    projects: list[ExtractedProject] = []
    for line in sections.get("projects", []):
        name, url = _org(line)
        if url is not None and _LINK.fullmatch(url):
            projects.append(
                ExtractedProject(
                    name=name,
                    description=None,
                    url=url,
                    start_date=None,
                    end_date=None,
                    highlights=[],
                )
            )
        elif projects:
            previous = projects[-1].description
            projects[-1].description = f"{previous} {line}" if previous else line

    skills: list[ExtractedSkill] = []
    category: str | None = None
    for line in sections.get("skills", []):
        label, colon, rest = line.partition(":")
        if colon and len(label) <= 40:
            category = label.strip()
            names = _split_list(rest)
        else:
            names = _split_list(line)
        skills.extend(ExtractedSkill(name=name, category=category) for name in names)

    summary = " ".join(sections.get("summary", [])) or None
    return ExtractedResume(
        basics=ExtractedBasics(
            name=header[0] if header else "",
            headline=None,
            email=email.group(0) if email else None,
            phone=phone.group(0) if phone else None,
            location=location,
            links=links,
        ),
        summary=summary,
        work=work,
        education=education,
        projects=projects,
        skills=skills,
    )


def extract_resume(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse | None:
    if _output_properties(info) != _EXTRACTED_KEYS:
        return None  # not a resume (the careers-page extractor shares the purpose)
    text = _block(_prompt_text(messages), "resume")
    if text is None:  # the vision path: page images only
        resume = ExtractedResume(
            basics=ExtractedBasics(
                name="Resume from page images",
                headline=None,
                email=None,
                phone=None,
                location=None,
                links=[],
            ),
            summary=None,
            work=[],
            education=[],
            projects=[],
            skills=[],
        )
    else:
        resume = parse_resume_text(text)
    return _answer(info, resume.dump())


# --- tailor ---------------------------------------------------------------------------------------

_TAILOR_KEYS = {"summary", "work", "education", "projects", "skills", "changes"}
_ITEM_HEADER = re.compile(r"^\[(?P<id>[^\]]+)\] ")


def _prose_items(resume_text: str) -> dict[str, dict[str, Any]]:
    """Per item id in `resume_to_text` prose: its indented summary/description line and its
    bullets."""
    items: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    for line in resume_text.splitlines():
        header = _ITEM_HEADER.match(line)
        if header:
            current = items.setdefault(header["id"], {"text": None, "highlights": []})
            current["dates_seen"] = False
        elif current is not None and line.startswith("  • "):
            current["highlights"].append(line[4:])
        elif current is not None and line.startswith("  "):
            if " · source: " in line and not current["dates_seen"]:
                current["dates_seen"] = True  # a role's date line
            elif current["text"] is None:
                current["text"] = line[2:]
        elif not line.strip():
            current = None
    return items


def tailor(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse | None:
    if _output_properties(info) != _TAILOR_KEYS:
        return None
    instructions = info.instructions or ""
    exact = json.loads(_block(instructions, "exact_fields") or "{}")
    prose = _block(instructions, "resume") or ""
    items = _prose_items(prose)
    summary = re.search(
        r"(?:^|\n\n)SUMMARY\n(.*?)(?=\n\n(?:EXPERIENCE|EDUCATION|PROJECTS|SKILLS)\n|\Z)",
        prose,
        re.DOTALL,
    )

    def prose_of(item_id: str) -> dict[str, Any]:
        return items.get(item_id, {"text": None, "highlights": []})

    work = [
        {
            **role,
            "summary": prose_of(role["id"])["text"],
            "highlights": prose_of(role["id"])["highlights"],
        }
        for role in exact.get("work", [])
    ]
    projects = [
        {
            "id": project["id"],
            "name": project["name"],
            "description": prose_of(project["id"])["text"],
            "url": project["url"],
            "startDate": project["startDate"],
            "endDate": project["endDate"],
            "highlights": prose_of(project["id"])["highlights"],
        }
        for project in exact.get("projects", [])
    ]
    return _answer(
        info,
        {
            "summary": summary.group(1) if summary else None,
            "work": work,
            "education": exact.get("education", []),
            "projects": projects,
            "skills": exact.get("skills", []),
            "changes": [],
        },
    )


# --- verify ---------------------------------------------------------------------------------------

_CLAIM_ID = re.compile(r'<claim id="([^"]+)">')


def verify(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse | None:
    if _output_properties(info) != {"verdicts"}:
        return None
    ids = _CLAIM_ID.findall(_prompt_text(messages))
    return _answer(
        info,
        {"verdicts": [{"claim_id": claim, "supported": True, "reason": None} for claim in ids]},
    )


# --- Registration ---------------------------------------------------------------------------------

OVERRIDES: dict[str, Override] = {"extract": extract_resume, "tailor": tailor, "verify": verify}


def register() -> None:
    """Registers these overrides with the fake registry (`apps.llm.fake`, step 3B)."""
    fake = importlib.import_module("apps.llm.fake")
    for purpose, override in OVERRIDES.items():
        fake.register(purpose, override)


# Registered on import when step 3B's registry is installed (it is absent in this lane's tree).
if importlib.util.find_spec("apps.llm.fake") is not None:
    register()
