"""User edits to the base resume, and new base versions (design §11.3, R14; a port of
`src/lib/resume/edits.ts`).

**Provenance is only useful if it is true.** Stamping every item `user` whenever the editor
saves would make a later LinkedIn merge refuse to touch anything (it never overwrites
`source: user`), and stamping nothing would let a merge silently undo a correction. So each item
is compared with what was stored and re-sourced only if its content actually differs.

**Canonical JSON.** The stored side comes back from a jsonb column, which does not keep key
order, so items are compared by a canonical serialisation: keys sorted, and None treated as
absent (a jsonb round trip turns one into the other).

**Versions.** A base resume is never overwritten: an edit, or an upload, is a new base version
(`max + 1`) written in one transaction. Two writers can pick the same number; the partial unique
constraint `resumes_base_version` refuses the second, which retries once with the next number.
Every reader takes the newest version through `queries.latest_base_resume` (R14).

**An item keeps its id across uploads.** A new file is read from scratch, but a resume variant
(`variants.py`) names the base's items by id, so `carry_ids` gives an item from the new file the
id of the one it clearly is in the resume before it: the same employer and title, the same school
and degree, the same project or skill name, the same link. Only the id is carried; the words and
the provenance are the new file's.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, Self

from django.db import IntegrityError, transaction
from django.db.models import Max

from apps.resumes import schema
from apps.resumes.models import Resume as ResumeRow
from apps.resumes.models import ResumeKind
from apps.resumes.queries import latest_base_resume

__all__ = [
    "SavedEdit",
    "canonical_json",
    "carry_ids",
    "create_base_version",
    "save_user_edit",
    "stamp_user_edits",
]

_PROVENANCE = ("id", "source")
_VERSION_CONSTRAINT = "resumes_base_version"


def _strip_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip_none(inner) for key, inner in value.items() if inner is not None}
    if isinstance(value, list):
        return [_strip_none(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    """Order-independent serialisation: keys sorted, None and absent alike."""
    return json.dumps(_strip_none(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class _Sourced(Protocol):
    """A resume item: links, roles, schools, projects and skills all carry these."""

    id: str
    source: schema.ItemSource

    def dump(self, **kwargs: Any) -> dict[str, Any]: ...

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self: ...


def _content(item: _Sourced) -> str:
    """An item's content, without the provenance fields themselves."""
    data = item.dump()
    for key in _PROVENANCE:
        data.pop(key, None)
    return canonical_json(data)


def _stamp_list[T: _Sourced](next_items: list[T], previous: list[T]) -> list[T]:
    by_id = {item.id: item for item in previous}
    stamped: list[T] = []
    for item in next_items:
        before = by_id.get(item.id)
        if before is None or _content(item) != _content(before):
            # A new item the user added, or one whose content they changed.
            stamped.append(item.model_copy(update={"source": "user"}))
        else:
            stamped.append(item.model_copy(update={"source": before.source}))
    return stamped


def stamp_user_edits(next_resume: schema.Resume, previous: schema.Resume | None) -> schema.Resume:
    """`next_resume` with `source: "user"` on every item the user added or edited, and the
    original provenance kept on everything they left alone. With no previous resume, nothing
    is compared and `next_resume` is returned as it is."""
    if previous is None:
        return next_resume
    return next_resume.model_copy(
        update={
            "basics": next_resume.basics.model_copy(
                update={"links": _stamp_list(next_resume.basics.links, previous.basics.links)}
            ),
            "work": _stamp_list(next_resume.work, previous.work),
            "education": _stamp_list(next_resume.education, previous.education),
            "projects": _stamp_list(next_resume.projects, previous.projects),
            "skills": _stamp_list(next_resume.skills, previous.skills),
        }
    )


# --- Ids across uploads --------------------------------------------------------------------------

# Everything a reader may add or drop between two readings of one line: spaces, case and
# punctuation. "+" and "#" stay, or C, C++ and C# would be one skill.
_NOT_A_WORD = re.compile(r"[^\w+#]|_")
_SCHEME = re.compile(r"^https?://(www\.)?")

type _Key = tuple[str, ...]


def _words(value: str | None) -> str:
    """What two readings of one line share: its letters and digits, in lower case."""
    return _NOT_A_WORD.sub("", (value or "").casefold())


def _work_key(item: schema.WorkItem) -> _Key:
    return (_words(item.company), _words(item.position))


def _education_key(item: schema.EducationItem) -> _Key:
    return (_words(item.institution), _words(item.study_type), _words(item.area))


def _project_key(item: schema.ProjectItem) -> _Key:
    return (_words(item.name),)


def _skill_key(item: schema.SkillItem) -> _Key:
    return (_words(item.name),)


def _link_key(item: schema.Link) -> _Key:
    return (_SCHEME.sub("", item.url.strip().casefold()).rstrip("/"),)


def _carry[T: _Sourced](
    next_items: list[T], previous: list[T], key: Callable[[T], _Key]
) -> list[T]:
    """`next_items`, each with the id of the previous item that has its key. Items that share a
    key (two stints in one role) pair off in the order they are listed, and an id is given once."""
    waiting: dict[_Key, list[str]] = {}
    for item in previous:
        waiting.setdefault(key(item), []).append(item.id)
    carried: list[T] = []
    for item in next_items:
        item_key = key(item)
        ids = waiting.get(item_key) if item_key[0] else None  # a nameless item is nobody's
        carried.append(item.model_copy(update={"id": ids.pop(0)}) if ids else item)
    return carried


def carry_ids(next_resume: schema.Resume, previous: schema.Resume | None) -> schema.Resume:
    """A freshly read resume with the ids of the items it shares with the one before it, so
    whatever names an item by id (a resume variant) still finds it. Items the previous resume
    does not have keep the new ids they were read with."""
    if previous is None:
        return next_resume
    return next_resume.model_copy(
        update={
            "basics": next_resume.basics.model_copy(
                update={"links": _carry(next_resume.basics.links, previous.basics.links, _link_key)}
            ),
            "work": _carry(next_resume.work, previous.work, _work_key),
            "education": _carry(next_resume.education, previous.education, _education_key),
            "projects": _carry(next_resume.projects, previous.projects, _project_key),
            "skills": _carry(next_resume.skills, previous.skills, _skill_key),
        }
    )


def _is_version_collision(error: IntegrityError) -> bool:
    diag = getattr(error.__cause__, "diag", None)
    return getattr(diag, "constraint_name", None) == _VERSION_CONSTRAINT


def create_base_version(
    user_id: str,
    resume: schema.Resume,
    *,
    source_document_id: uuid.UUID | None = None,
) -> ResumeRow:
    """Writes `resume` as the user's next base version, in one transaction (a savepoint when
    the caller already has one open, so an upload can store its document text alongside).
    Retried once when another writer took the same version number."""
    for attempt in (1, 2):
        try:
            with transaction.atomic():
                newest = (
                    ResumeRow.objects.for_user(user_id)
                    .filter(kind=ResumeKind.BASE)
                    .aggregate(newest=Max("version"))["newest"]
                )
                return ResumeRow.objects.create_for(
                    user_id,
                    kind=ResumeKind.BASE,
                    version=(newest or 0) + 1,
                    data=resume.dump(),
                    source_document_id=source_document_id,
                )
        except IntegrityError as error:
            if attempt == 2 or not _is_version_collision(error):
                raise
    raise AssertionError("unreachable")  # pragma: no cover


@dataclass(frozen=True, slots=True)
class SavedEdit:
    row: ResumeRow | None  # the newest base version after the save; None only if there was none
    changed: bool  # False: nothing differed, so no version was written


def save_user_edit(user_id: str, edited: schema.Resume) -> SavedEdit:
    """`PUT /api/resume`: stamps the user's edits, and writes a new base version unless nothing
    changed (a no-op save writes nothing)."""
    previous = latest_base_resume(user_id)
    stamped = stamp_user_edits(edited, previous.resume if previous else None)
    if previous is not None and canonical_json(stamped.dump()) == canonical_json(
        previous.resume.dump()
    ):
        return SavedEdit(
            row=ResumeRow.objects.for_user(user_id).filter(id=previous.id).first(), changed=False
        )
    return SavedEdit(row=create_base_version(user_id, stamped), changed=True)
