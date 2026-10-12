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
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
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
