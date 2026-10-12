"""The one source of resume facts (design §11.3, R14).

Scoring, deep review, tailoring, the chat agent and onboarding all read the user's newest base
resume through `latest_base_resume(user_id)`. There is no mirror to drift: the old
`profiles.data` copy is gone, and edits land as a new base version. Sync, like every ORM
function; async callers use `await db(latest_base_resume, user_id)`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from apps.resumes import schema
from apps.resumes.models import Resume, ResumeKind


@dataclass(frozen=True, slots=True)
class BaseResume:
    id: uuid.UUID
    version: int
    resume: schema.Resume


def latest_base_resume_row(user_id: str) -> Resume | None:
    """The newest base version's row, whether or not its data reads."""
    return (
        Resume.objects.for_user(user_id).filter(kind=ResumeKind.BASE).order_by("-version").first()
    )


def latest_base_resume(user_id: str) -> BaseResume | None:
    """The newest base version, parsed. None when the user has none, or when its data does
    not read (as `loadBaseResume` did)."""
    row = latest_base_resume_row(user_id)
    if row is None:
        return None
    resume = schema.Resume.read(row.data)
    if resume is None:
        return None
    return BaseResume(id=row.id, version=row.version, resume=resume)
