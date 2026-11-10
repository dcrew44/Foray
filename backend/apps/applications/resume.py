"""Which resume an application sends (#61).

A user keeps a few variants of their resume, one per kind of role, and each application sends
one of them. The choice is deterministic (`resumes.variants.resume_for`: the variant whose
keywords the posting's title says, then the default, then the full resume) and it is shown on
the application with the reason, so the user can change it:

    assign(user_id, application)        make or refresh the automatic choice (when it is planned)
    set_choice(user_id, id, choice)     the user's own: a variant's id, "base", or "" for automatic
    document_for(user_id, application)  the PDF that choice prints to, when one is on file
    ensure_pdf(user_id, application)    start a render when there is none, or it is out of date

`applications.resume_choice` is the user's word ("" until they say otherwise) and
`applications.variant` what follows from it, so the Apply page can name the variant without
working the choice out again. A variant deleted later falls back to the automatic choice the
next time the application is planned.
"""

from __future__ import annotations

import logging
import uuid

from django.db.models.functions import Now

from apps.applications.models import Application
from apps.resumes import pipeline, variants
from apps.resumes.edits import canonical_json
from apps.resumes.models import Document, ResumeKind, ResumeVariant
from apps.resumes.models import Resume as ResumeRow
from apps.resumes.queries import latest_base_resume, latest_base_resume_row

__all__ = ["AUTOMATIC", "FULL_RESUME", "assign", "document_for", "ensure_pdf", "set_choice"]

log = logging.getLogger("foray.applications")

AUTOMATIC = ""
FULL_RESUME = variants.FULL_RESUME
NO_RESUME = "There is no resume on file yet."


def _title(application: Application) -> str:
    return application.match.job.title


def assign(user_id: str, application: Application) -> None:
    """Works the choice out for this application and stores it with its reason. Sync."""
    chosen = variants.resume_for(user_id, _title(application), application.resume_choice or None)
    variant = chosen.variant if chosen is not None else None
    reason = chosen.reason if chosen is not None else NO_RESUME
    if (application.variant_id, application.resume_reason) == (
        variant.id if variant else None,
        reason,
    ):
        return
    Application.objects.for_user(user_id).filter(id=application.id).update(
        variant=variant, resume_reason=reason, updated_at=Now()
    )
    application.variant = variant
    application.resume_reason = reason


def set_choice(user_id: str, application_id: uuid.UUID | str, choice: str) -> bool:
    """The user's own choice for one application: a variant's id, `FULL_RESUME`, or
    `AUTOMATIC` to let the title decide again. False when the application is not theirs or the
    variant is not one of theirs. Sync."""
    application = (
        Application.objects.for_user(user_id)
        .filter(id=application_id)
        .select_related("match__job")
        .first()
    )
    if application is None:
        return False
    if choice not in (AUTOMATIC, FULL_RESUME):
        try:
            wanted = uuid.UUID(choice)
        except ValueError:
            return False
        if not ResumeVariant.objects.for_user(user_id).filter(id=wanted).exists():
            return False
        choice = str(wanted)
    Application.objects.for_user(user_id).filter(id=application.id).update(
        resume_choice=choice, updated_at=Now()
    )
    application.resume_choice = choice
    assign(user_id, application)
    ensure_pdf(user_id, application)
    return True


def _variant_pdf(user_id: str, variant: ResumeVariant) -> tuple[Document | None, bool]:
    """A variant's newest PDF, and whether a render today would print the same file."""
    view = next(
        (
            view
            for view in variants.variant_views(user_id, _base(user_id))
            if view.variant.id == variant.id
        ),
        None,
    )
    if view is None or view.document_id is None:
        return None, False
    document = Document.objects.for_user(user_id).filter(id=view.document_id).first()
    return document, view.current


def _base(user_id: str):  # noqa: ANN202 (a resumes.schema.Resume, or None)
    base = latest_base_resume(user_id)
    return base.resume if base is not None else None


def _base_pdf(user_id: str) -> tuple[Document | None, bool]:
    """The newest base version's PDF if it has one, else the newest older one, and whether it
    is of the newest version."""
    newest = latest_base_resume_row(user_id)
    if newest is None:
        return None, False
    rendered = (
        ResumeRow.objects.for_user(user_id)
        .filter(kind=ResumeKind.BASE, rendered_document__isnull=False)
        .order_by("-version")
        .select_related("rendered_document")
        .first()
    )
    if rendered is None:
        return None, False
    current = rendered.id == newest.id or canonical_json(rendered.data) == canonical_json(
        newest.data
    )
    return rendered.rendered_document, current


def _pdf(user_id: str, application: Application) -> tuple[Document | None, bool]:
    if application.variant is not None:
        return _variant_pdf(user_id, application.variant)
    return _base_pdf(user_id)


def document_for(user_id: str, application: Application) -> Document | None:
    """The PDF this application attaches: the chosen variant's own, or the full resume's.
    The newest on file even when it is a little behind; `ensure_pdf` brings it up to date.
    Sync."""
    return _pdf(user_id, application)[0]


def ensure_pdf(user_id: str, application: Application) -> bool:
    """Starts a render of the chosen resume when no PDF is on file or the one on file is out
    of date. True when a render was started. Never raises: an application is worth having
    without its PDF, and the Profile page can always make one. Sync."""
    try:
        _document, current = _pdf(user_id, application)
        if current:
            return False
        if application.variant is not None:
            started = pipeline.render_variant(user_id, application.variant.id)
        else:
            started = pipeline.render_base_resume(user_id)
        return bool(started.ok)
    except Exception:
        log.warning("could not start a resume render for %s", application.id, exc_info=True)
        return False
