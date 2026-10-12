"""A resume as prose, for a model to read (design §11.3; a port of `src/lib/resume/text.ts`).

Models read this rather than the JSON: a resume is prose, and the JSON's nesting and provenance
fields cost tokens without helping comprehension. Every item keeps its id inline in brackets,
`[id] Position — Company`, so whatever reads it (the chat agent, the deep evaluation, the
tailor) can cite the exact base fact a claim rests on.

The separators are U+2014 (—), U+2013 (–) and U+00B7 (·), exactly as today: the text is pinned
by `goldens/strings/resume_text.json` and sits inside prompts pinned by the §8.10 goldens.
"""

from __future__ import annotations

from collections.abc import Iterable

from apps.resumes.schema import Resume

# JavaScript's `\s` and `String.prototype.trim()` set (WhiteSpace and LineTerminator). Python's
# `str.strip()` and `\s` add U+001C-U+001F and U+0085 and leave out U+FEFF; the ported rules use
# this set so they trim and split as they did.
JS_SPACE = (
    "\t\n\v\f\r \xa0\u1680"
    + "".join(chr(code) for code in range(0x2000, 0x200B))
    + "\u2028\u2029\u202f\u205f\u3000\ufeff"
)


def js_trim(value: str) -> str:
    """`String.prototype.trim()`."""
    return value.strip(JS_SPACE)


RESUME_SECTIONS: tuple[str, ...] = (
    "basics",
    "summary",
    "work",
    "education",
    "projects",
    "skills",
)


def _lines(*parts: str | None) -> str:
    """`[...].filter(Boolean).join("\\n")`."""
    return "\n".join(part for part in parts if part)


def resume_to_text(resume: Resume, sections: Iterable[str] = RESUME_SECTIONS) -> str:
    wanted = set(sections)
    parts: list[str] = []

    if "basics" in wanted:
        basics = resume.basics
        parts.append(
            _lines(
                f"Name: {basics.name or '(unknown)'}",
                basics.headline and f"Headline: {basics.headline}",
                basics.location and f"Location: {basics.location}",
                basics.email and f"Email: {basics.email}",
                "Links: " + ", ".join(f"{link.label} {link.url}" for link in basics.links)
                if basics.links
                else None,
            )
        )

    if "summary" in wanted and resume.summary:
        parts.append(f"SUMMARY\n{resume.summary}")

    if "work" in wanted and resume.work:
        parts.append(
            "EXPERIENCE\n"
            + "\n\n".join(
                _lines(
                    f"[{role.id}] {role.position} — {role.company}"
                    + (f" ({role.location})" if role.location else ""),
                    f"  {_or(role.start_date, '?')} – {_or(role.end_date, 'present')}"
                    f" · source: {role.source}",
                    role.summary and f"  {role.summary}",
                    *(f"  • {highlight}" for highlight in role.highlights),
                )
                for role in resume.work
            )
        )

    if "education" in wanted and resume.education:
        parts.append(
            "EDUCATION\n"
            + "\n".join(
                f"[{item.id}] {_or(item.study_type, '')} {_or(item.area, '')} — {item.institution}"
                f" ({_or(item.start_date, '?')} – {_or(item.end_date, '?')})"
                + (f" · {item.score}" if item.score else "")
                for item in resume.education
            )
        )

    if "projects" in wanted and resume.projects:
        parts.append(
            "PROJECTS\n"
            + "\n\n".join(
                _lines(
                    f"[{project.id}] {project.name}" + (f" — {project.url}" if project.url else ""),
                    project.description and f"  {project.description}",
                    *(f"  • {highlight}" for highlight in project.highlights),
                )
                for project in resume.projects
            )
        )

    if "skills" in wanted and resume.skills:
        grouped: dict[str, list[str]] = {}
        for skill in resume.skills:
            grouped.setdefault(_or(skill.category, "Other"), []).append(skill.name)
        parts.append(
            "SKILLS\n"
            + "\n".join(f"{category}: {', '.join(names)}" for category, names in grouped.items())
        )

    return "\n\n".join(parts)


def _or(value: str | None, fallback: str) -> str:
    """JS `value ?? fallback`: only None falls back; an empty string is kept."""
    return fallback if value is None else value
