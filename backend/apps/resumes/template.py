"""The resume as a printable HTML document (design §11.5; a port of
`src/lib/resume/render/template.ts`).

`templates/resume.html` is a Django template with autoescape on; this module builds its context.
The same HTML is rendered to PDF by the worker (`render.py`) and served as the instant preview,
so an unescaped `<script>` in a bullet would be stored XSS: every value reaches the page through
Django's escaping, which is stronger than hand-escaping each interpolation. Only `safe_href`
decides what may become a link.

The layout is deliberately ATS-friendly (one column, real text in reading order, no tables,
icons or images), because the first reader of a resume is usually a parser. Everything the
resume says comes from the stored data verbatim, dates included; this module only decides how it
looks. The output is pinned by `goldens/template/*.html` (one declared difference: Django escapes
`'` as `&#x27;` where the TS template wrote `&#39;`).

Lengths are Python-native (design §10.3); whitespace is JavaScript's `\\s`, so `trim()` and the
link rules behave as they did.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from django.template.loader import render_to_string

from apps.resumes.schema import EducationItem, ProjectItem, Resume, SkillItem, WorkItem
from apps.resumes.text import JS_SPACE, js_trim

__all__ = [
    "CONTACT_LINE_CHARS",
    "Density",
    "date_range",
    "display_url",
    "render_resume_html",
    "safe_href",
]

Density = Literal["normal", "compact"]

# Roughly how many characters of 9 pt contact text fit across the page. Used only to decide
# whether links get their own line: a deliberate break reads as designed, where a wrap mid-list
# leaves a separator dangling at the end of the first line.
CONTACT_LINE_CHARS = 100

_S = f"[{re.escape(JS_SPACE)}]"
_NOT_S = f"[^{re.escape(JS_SPACE)}]"
# The JS patterns have no `u` flag, so `i` folds ASCII only and `\d` is ASCII: re.ASCII.
_FLAGS = re.ASCII | re.IGNORECASE
_HAS_SPACE = re.compile(_S)
_HTTP = re.compile(r"^https?://", _FLAGS)
_MAILTO = re.compile(rf"^mailto:[^{re.escape(JS_SPACE)}@]+@[^{re.escape(JS_SPACE)}@]+\Z", _FLAGS)
_BARE_EMAIL = re.compile(
    rf"^[^{re.escape(JS_SPACE)}@/:]+@[^{re.escape(JS_SPACE)}@/:]+\.[a-z]{{2,}}\Z", _FLAGS
)
_OTHER_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:(?!\d)", _FLAGS)
_BARE_DOMAIN = re.compile(
    rf"^(www\.)?[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{{2,}}(:\d+)?([/?#]{_NOT_S}*)?\Z", _FLAGS
)
_NOT_PHONE = re.compile(r"[^0-9+]")


def safe_href(raw: str | None) -> str | None:
    """A link target that is safe to put in an href, or None.

    Resumes store links the way people write them ("github.com/someone"), rarely with a scheme,
    so a bare domain becomes https. Any scheme other than http(s) or mailto (javascript:, data:,
    file:) is refused, and the value is rendered as plain text instead."""
    value = js_trim(raw or "")
    if not value or _HAS_SPACE.search(value):
        return None
    if _HTTP.match(value):
        return value
    if _MAILTO.match(value):
        return value
    if _BARE_EMAIL.match(value):
        return f"mailto:{value}"
    # Any other scheme ("javascript:", "data:"), but not a port ("host:8080").
    if _OTHER_SCHEME.match(value):
        return None
    if _BARE_DOMAIN.match(value):
        return f"https://{value}"
    return None


_DISPLAY_PREFIXES = (
    re.compile(r"^https?://", _FLAGS),
    re.compile(r"^mailto:", _FLAGS),
    re.compile(r"^www\.", _FLAGS),
)
_TRAILING_SLASHES = re.compile(r"/+\Z")


def display_url(raw: str) -> str:
    """ "https://www.github.com/x/" -> "github.com/x": what a reader should see."""
    value = js_trim(raw)
    for prefix in _DISPLAY_PREFIXES:
        value = prefix.sub("", value, count=1)
    return _TRAILING_SLASHES.sub("", value)


def date_range(start: str | None, end: str | None) -> str:
    """Dates exactly as stored: "March 2023 – Present", or whichever end exists."""
    start = js_trim(start) if start is not None else None
    end = js_trim(end) if end is not None else None
    if start and end:
        return start if start == end else f"{start} – {end}"
    return start or end or ""


# --- The context ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Part:
    """One piece of an inline line (a title or a contact line), joined by separators.

    kind `role` is `<strong class="role">`, `where` is `<span class="where">`, `text` is bare
    text; any of them is a link when `href` is set."""

    kind: Literal["text", "role", "where"]
    text: str
    href: str | None = None


@dataclass(frozen=True, slots=True)
class _Entry:
    title: list[_Part]
    dates: str
    summary: str = ""
    bullets: list[str] = field(default_factory=list)


def _present(value: str | None) -> bool:
    return bool(value and js_trim(value))


def _text(value: str | None) -> str:
    return js_trim(value) if value else ""


def _link(url: str, label: str | None = None) -> _Part:
    return _Part("text", js_trim(label or "") or display_url(url), safe_href(url))


def _keep(part: _Part) -> bool:
    """`inline()` drops empty strings; a role is markup, so it is never empty."""
    return part.kind == "role" or bool(part.text) or part.href is not None


def _inline(parts: list[_Part | None]) -> list[_Part]:
    return [part for part in parts if part is not None and _keep(part)]


def _phone(value: str) -> _Part:
    digits = _NOT_PHONE.sub("", value)
    return _Part("text", _text(value), f"tel:{digits}" if len(digits) >= 7 else None)


def _contact_lines(resume: Resume) -> list[list[_Part]]:
    basics = resume.basics
    personal: list[tuple[_Part, int]] = []
    if basics.location is not None and _present(basics.location):
        location = js_trim(basics.location)
        personal.append((_Part("text", location), len(location)))
    if basics.email is not None and _present(basics.email):
        email = js_trim(basics.email)
        personal.append((_link(email, basics.email), len(email)))
    if basics.phone is not None and _present(basics.phone):
        personal.append((_phone(basics.phone), len(js_trim(basics.phone))))

    links = [
        (_link(link.url), len(display_url(link.url))) for link in basics.links if _present(link.url)
    ]

    def width(parts: list[tuple[_Part, int]]) -> int:
        return sum(chars + 3 for _, chars in parts)

    if personal and links and width(personal + links) > CONTACT_LINE_CHARS:
        lines = [personal, links]
    else:
        lines = [personal + links]
    return [_inline([part for part, _ in line]) for line in lines if line]


def _work(item: WorkItem) -> _Entry:
    return _Entry(
        title=_inline(
            [
                _Part("role", _text(item.position)) if _present(item.position) else None,
                _Part("text", _text(item.company)),
                _Part("where", _text(item.location)) if _present(item.location) else None,
            ]
        ),
        dates=date_range(item.start_date, item.end_date),
        summary=_text(item.summary),
        bullets=_bullets(item.highlights),
    )


def _degree(item: EducationItem) -> str:
    study_type = js_trim(item.study_type) if item.study_type is not None else ""
    area = js_trim(item.area) if item.area is not None else ""
    if study_type and area:
        return f"{study_type} in {area}"
    return study_type or area


def _education(item: EducationItem) -> _Entry:
    qualification = _degree(item)
    return _Entry(
        title=_inline(
            [
                _Part("role", qualification)
                if qualification
                else _Part("role", _text(item.institution)),
                _Part("text", _text(item.institution)) if qualification else None,
                _Part("where", _text(item.score)) if _present(item.score) else None,
            ]
        ),
        dates=date_range(item.start_date, item.end_date),
        bullets=_bullets(item.highlights),
    )


def _project(item: ProjectItem) -> _Entry:
    url = item.url if item.url is not None and _present(item.url) else None
    where = None
    if url is not None:
        link = _link(url)
        where = _Part("where", link.text, link.href)
    return _Entry(
        title=_inline([_Part("role", _text(item.name)), where]),
        dates=date_range(item.start_date, item.end_date),
        summary=_text(item.description),
        bullets=_bullets(item.highlights),
    )


def _bullets(items: list[str]) -> list[str]:
    return [js_trim(item) for item in items if _present(item)]


def _skills(items: list[SkillItem]) -> dict[str, Any] | None:
    """Skills in the resume's own grouping, in order of first appearance, so a tailored resume
    that leads with its most relevant skill leads with that skill's group too. Uncategorised
    skills get one plain line, or an "Other" row when they sit beside labelled groups."""
    kept = [skill for skill in items if _present(skill.name)]
    if not kept:
        return None
    groups: dict[str, list[str]] = {}
    for skill in kept:
        key = js_trim(skill.category) if skill.category is not None else ""
        groups.setdefault(key, []).append(js_trim(skill.name))
    if list(groups) == [""]:
        return {"skills_line": [js_trim(skill.name) for skill in kept]}
    rows = sorted(groups.items(), key=lambda group: group[0] == "")  # stable: "" goes last
    return {"skill_rows": [{"label": label or "Other", "names": names} for label, names in rows]}


def _context(resume: Resume, title: str | None, density: Density) -> dict[str, Any]:
    basics = resume.basics
    name = js_trim(basics.name or "")
    sections: list[dict[str, Any]] = []
    if resume.summary is not None and _present(resume.summary):
        sections.append({"id": "summary", "heading": "Summary", "summary": _text(resume.summary)})
    for section_id, heading, entries in (
        ("experience", "Experience", [_work(item) for item in resume.work]),
        ("projects", "Projects", [_project(item) for item in resume.projects]),
        ("education", "Education", [_education(item) for item in resume.education]),
    ):
        if entries:
            sections.append({"id": section_id, "heading": heading, "entries": entries})
    skills = _skills(resume.skills)
    if skills is not None:
        sections.append({"id": "skills", "heading": "Skills", **skills})

    return {
        "title": title if title is not None else (f"{name} — Resume" if name else "Resume"),
        "density_class": " compact" if density == "compact" else "",
        "name": name or "Resume",
        "headline": _text(basics.headline),
        "contact_lines": _contact_lines(resume),
        "sections": sections,
    }


def render_resume_html(
    resume: Resume, *, title: str | None = None, density: Density = "normal"
) -> str:
    """A complete HTML document with inline CSS and an `@page` Letter rule.

    `title` defaults to "<Name> — Resume": Chromium writes it into the PDF's metadata, where a
    recruiter's viewer may show it, so it never mentions tailoring. `density="compact"` trims
    type size and spacing slightly and never changes a word of content; the PDF renderer uses
    it when the normal layout runs just past a page."""
    return render_to_string("resume.html", _context(resume, title, density))
