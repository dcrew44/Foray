"""What the browser extension is given and what it reports back (#22, #23, #24).

Applying happens in the user's own browser, under their own identity: the extension asks Foray
for a prepared application, puts the planned answers into the employer's form, and stops. The
user reads the form and presses submit. This module is the server's half of that:

- **Signing in.** The extension has no session cookie. It carries a token the user made in
  Settings (`new_token`), sent as `Authorization: Bearer`; only its SHA-256 is stored
  (`authenticate`). A token reads and reports on its own user's applications and nothing else.
- **`payload`**: one application as the extension fills it: every planned field with its value,
  the resume to attach, and `blocked` when Foray will not fill this one (`caps`).
- **`plan_fields`**: for a form Foray could not read ahead, the extension sends the fields it
  found on the page and gets the same planner's answers back.
- **`record_filled` / `record_submitted`**: what was put in the form, and, once the user has
  sent it, the proof that it arrived: the confirming page's address and words and a picture
  of it, kept on the application for good.

**Submitting (#40).** By default the extension never presses submit: the user does. With the
`auto_submit` setting on, `may_submit` allows it for one application at a time, and only when
all of this holds: the match is in the automated lane, the application is `ready`, every
answer came from the user's resume or saved answers (or is a declined disclosure, the resume
file, or an optional field left empty), and the limits below are not reached. Anything else,
and the user presses submit as before. Autopilot does not turn this on.

**Restraint (#24)** is enforced here, not in the extension: Foray fills at most
`apply_per_employer` applications to one employer in sixty days and `apply_daily_cap` in a day.
The user can always apply by hand; Foray just will not help with the third.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

from django.db import transaction
from django.db.models.functions import Now

from apps.accounts.models import User
from apps.accounts.schemas import Settings
from apps.applications import prepare, resume
from apps.applications.actions import SUBMITTED_EVENT
from apps.applications.forms import FormField
from apps.applications.models import Application, ApplicationStatus, ExtensionToken
from apps.core.storage import storage, storage_key
from apps.matching import queries as match_queries
from apps.matching.lanes import Lane, lane_of
from apps.matching.models import Stage
from apps.resumes.models import Document
from apps.runs.events import emit

__all__ = [
    "MAX_TOKENS",
    "PREFIX",
    "authenticate",
    "blocked_reason",
    "find_for_url",
    "may_submit",
    "new_token",
    "payload",
    "plan_fields",
    "record_filled",
    "record_submitted",
    "resume_document",
]

PREFIX = "foray_ext_"
MAX_TOKENS = 5
EMPLOYER_WINDOW = timedelta(days=60)
S = ApplicationStatus
FILLABLE = (S.NEEDS_ANSWERS, S.READY)
CONFIRMATION_MAX = 4000
IMAGE_MAX_BYTES = 3 * 1024 * 1024
_PNG = b"\x89PNG\r\n\x1a\n"
_JPEG = b"\xff\xd8\xff"
_GREENHOUSE_ID = re.compile(r"/jobs/(\d+)")


# --- signing in ----------------------------------------------------------------------------------


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(user_id: str, label: str = "") -> tuple[ExtensionToken, str] | None:
    """A new token for this user and its secret, which is shown once and never stored. None
    when they already have as many as anyone needs. Sync."""
    with transaction.atomic():
        if ExtensionToken.objects.for_user(user_id).count() >= MAX_TOKENS:
            return None
        secret = PREFIX + secrets.token_urlsafe(32)
        row = ExtensionToken.objects.create_for(
            user_id, token_hash=_digest(secret), label=" ".join(label.split())[:80]
        )
    return row, secret


def authenticate(secret: str) -> User | None:
    """The user a token belongs to, or None. Notes the use at most once a minute. Sync."""
    if not secret.startswith(PREFIX):
        return None
    token = (
        ExtensionToken.objects.unscoped("a bearer token is how its user is found")
        .filter(token_hash=_digest(secret))
        .select_related("user")
        .first()
    )
    if token is None:
        return None
    ExtensionToken.objects.for_user(token.user_id).filter(id=token.id).exclude(
        last_used_at__gt=Now() - timedelta(minutes=1)
    ).update(last_used_at=Now())
    return token.user


# --- restraint -----------------------------------------------------------------------------------


def blocked_reason(user: User, application: Application) -> str | None:
    """Why Foray will not fill this application, or None when it will. Counts what the user
    has sent: to this employer in sixty days, and to anyone today. Sync; two queries."""
    limits = Settings.read(user.settings)
    job = application.match.job
    sent = (
        Application.objects.for_user(user.pk).filter(status=S.SUBMITTED).exclude(id=application.id)
    )
    employer = (
        sent.filter(match__job__company_id=job.company_id)
        if job.company_id
        else sent.filter(match__job__company_name=job.company_name)
    )
    recent = employer.filter(submitted_at__gte=Now() - EMPLOYER_WINDOW).count()
    if recent >= limits.apply_per_employer:
        return (
            f"You have sent {recent} applications to {job.company_name} in the last 60 days, "
            "which is the limit you set. You can still apply yourself."
        )
    today = sent.filter(submitted_at__gte=Now() - timedelta(days=1)).count()
    if today >= limits.apply_daily_cap:
        return (
            f"You have sent {today} applications in the last 24 hours, which is the daily limit "
            "you set. You can still apply yourself."
        )
    return None


# The sources an answer may have for the extension to send it unattended: all of them are the
# user's own words or facts, or nothing at all. A source added later is not on this list until
# someone decides it should be.
OWN_ANSWERS = frozenset({"profile", "saved", "declined", "file", "skipped"})


def may_submit(user: User, application: Application) -> bool:
    """Whether the extension may press submit on this application itself (see the module
    docstring). Sync."""
    if not Settings.read(user.settings).auto_submit:
        return False
    if application.status != S.READY:
        return False
    if lane_of(application.match) != Lane.AUTOMATED:
        return False
    answers = [answer for answer in application.answers if isinstance(answer, dict)]
    if not answers or any(answer.get("source") not in OWN_ANSWERS for answer in answers):
        return False
    return blocked_reason(user, application) is None


# --- what the extension is given -----------------------------------------------------------------


def resume_document(user_id: str, application: Application) -> Document | None:
    """The PDF this application attaches: its chosen variant's own, or the full resume's
    (`applications/resume.py`). Sync."""
    return resume.document_for(user_id, application)


def _mine(user_id: str, application_id: uuid.UUID | str) -> Application | None:
    return (
        Application.objects.for_user(user_id)
        .filter(id=application_id)
        .select_related("match__job")
        .first()
    )


def payload(user: User, application: Application) -> dict[str, Any]:
    """One application as the extension fills it. Sync."""
    job = application.match.job
    document = resume_document(user.pk, application)
    answers = [answer for answer in application.answers if isinstance(answer, dict)]
    return {
        "id": application.id,
        "status": application.status,
        "company": job.company_name,
        "title": job.title,
        "apply_url": application.apply_url or job.apply_url or job.url,
        "form_source": application.form_source,
        "form_read": bool(application.questions) and application.form_source == "greenhouse",
        "fields": answers,
        "missing": prepare.missing_of(answers),
        "resume": {"filename": document.filename, "size_bytes": document.size_bytes}
        if document is not None
        else None,
        "blocked": blocked_reason(user, application)
        if application.status in FILLABLE
        else "This application is not waiting to be sent.",
        "may_submit": may_submit(user, application),
    }


def _same_page(candidate: str, url: str) -> bool:
    try:
        a, b = urlsplit(candidate), urlsplit(url)
    except ValueError:
        return False
    return (
        bool(a.hostname) and a.hostname == b.hostname and a.path.rstrip("/") == b.path.rstrip("/")
    )


def find_for_url(user_id: str, url: str) -> Application | None:
    """The open application whose form this page is: the same address as the application's
    apply link or its posting, or (Greenhouse, whose forms also live on employers' own sites as
    `?gh_jid=`) the same job id. Sync."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    job_id = (parse_qs(parts.query).get("gh_jid") or [None])[0]
    in_path = _GREENHOUSE_ID.search(parts.path)
    greenhouse_id = job_id or (in_path.group(1) if in_path else None)
    rows = (
        Application.objects.for_user(user_id)
        .filter(status__in=FILLABLE)
        .select_related("match__job")
        .order_by("-created_at")[:200]
    )
    for application in rows:
        job = application.match.job
        if any(
            _same_page(link, url)
            for link in (application.apply_url, job.apply_url, job.url)
            if link
        ):
            return application
        if greenhouse_id and job.source == "greenhouse" and job.external_id == greenhouse_id:
            return application
    return None


# --- what the extension reports ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PageField:
    key: str
    label: str
    type: str = "text"
    required: bool = False
    options: tuple[str, ...] = ()


def plan_fields(user: User, application: Application, fields: list[PageField]) -> dict[str, Any]:
    """Plans the fields the extension read from a form Foray could not read ahead, keeps them
    as the application's questions, and answers with the payload. A form that was read ahead
    keeps the board's own account of itself. Sync."""
    if application.status not in FILLABLE or (
        application.questions and application.form_source == "greenhouse"
    ):
        return payload(user, application)
    read = [
        FormField(
            key=field.key[:200],
            label=" ".join(field.label.split())[:500],
            type=field.type
            if field.type in ("text", "textarea", "select", "multiselect", "file")
            else "text",  # type: ignore[arg-type]
            required=field.required,
            options=[" ".join(option.split())[:300] for option in field.options][:200],
        )
        for field in fields[:200]
        if field.key and field.label.strip()
    ]
    if read:
        profile, saved = prepare._planning_inputs(user.pk)
        planned = prepare.plan_form(read, profile, saved)
        status = S.NEEDS_ANSWERS if prepare.missing_of(planned) else S.READY
        Application.objects.for_user(user.pk).filter(id=application.id, status__in=FILLABLE).update(
            questions=[field.dump() for field in read],
            answers=[item.dump() for item in planned],
            status=status,
            updated_at=Now(),
        )
        application.refresh_from_db()
    return payload(user, application)


def _sent(values: Any) -> list[dict[str, str]]:
    """What the extension says went into the form, trimmed to what is worth keeping."""
    kept: list[dict[str, str]] = []
    for item in values if isinstance(values, list) else []:
        if isinstance(item, dict) and item.get("key"):
            kept.append(
                {
                    "key": str(item["key"])[:200],
                    "label": str(item.get("label") or "")[:500],
                    "value": str(item.get("value") or "")[:4000],
                }
            )
    return kept[:300]


def record_filled(user_id: str, application_id: uuid.UUID | str, filled: Any) -> bool:
    """The extension filled the form: what went in, and when. Sync."""
    application = _mine(user_id, application_id)
    if application is None:
        return False
    attached = resume_document(user_id, application)
    return bool(
        Application.objects.for_user(user_id)
        .filter(id=application_id, status__in=FILLABLE)
        .update(
            sent_answers=_sent(filled),
            document=attached,
            filled_at=Now(),
            opened_at=Now(),
            updated_at=Now(),
        )
    )


def _image(data_url: str) -> tuple[bytes, str] | None:
    """A screenshot sent as a data URL, when it is a PNG or JPEG of a sane size."""
    head, _, encoded = data_url.partition(",")
    if not head.startswith("data:image/") or ";base64" not in head or not encoded:
        return None
    try:
        raw = base64.b64decode(encoded, validate=True)
    except ValueError:
        return None
    if len(raw) > IMAGE_MAX_BYTES:
        return None
    if raw.startswith(_PNG):
        return raw, "png"
    if raw.startswith(_JPEG):
        return raw, "jpg"
    return None


def record_submitted(
    user_id: str,
    application_id: uuid.UUID | str,
    *,
    url: str = "",
    text: str = "",
    screenshot: str = "",
    filled: Any = None,
    auto: bool = False,
) -> bool:
    """The user sent the application and the employer's page confirmed it: keep the proof and
    move the match to Applied. A second report for the same application changes nothing.
    Sync."""
    with transaction.atomic():
        application = (
            Application.objects.for_user(user_id)
            .select_for_update(of=("self",))
            .filter(id=application_id, status__in=FILLABLE)
            .select_related("match__job")
            .first()
        )
        if application is None:
            return False
        # "On its own" is the server's word, not the extension's: only when the rule allowed it.
        user = User.objects.get(pk=user_id)
        on_its_own = auto and may_submit(user, application)
        image_key = ""
        image = _image(screenshot) if screenshot else None
        if image is not None:
            image_key = storage_key(user_id, "application_proof", f"{application.id}.{image[1]}")
            storage().put(image_key, image[0])
        fields: dict[str, Any] = {
            "status": S.SUBMITTED,
            "submitted_at": Now(),
            "confirmation_url": url[:2000],
            "confirmation_text": " ".join(text.split())[:CONFIRMATION_MAX],
            "confirmation_image": image_key,
            "auto_submitted": on_its_own,
            "updated_at": Now(),
        }
        if filled is not None:
            fields["sent_answers"] = _sent(filled)
        if application.document_id is None:
            fields["document"] = resume_document(user_id, application)
        Application.objects.for_user(user_id).filter(id=application.id).update(**fields)
        match_queries.set_match_stage(user_id, application.match_id, Stage.APPLIED)
        job = application.match.job
        emit(
            user_id,
            SUBMITTED_EVENT,
            f"Foray sent your application to {job.company_name} for {job.title} on its own."
            if on_its_own
            else f"You sent your application to {job.company_name} for {job.title}.",
            data={"applicationId": str(application.id), "auto": on_its_own},
        )
    return True
