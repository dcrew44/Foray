"""Preparing an application ahead of the user (#60).

A match used to end in a list, with everything after it done by hand. Now a posting worth
applying to arrives with its application planned: the form's questions read from the board, an
answer for each, and what is still missing named.

    start(user_id, match_id)      a row in `preparing`, and the task that fills it, in one
                                  transaction (the watch calls `start_many` for what it kept)
    prepare(application_id)       the task: read the form, plan the answers, say what is missing
    replan(user_id)               after the user saves answers: plan again from the stored
                                  questions, with no fetch; what is now complete becomes ready

**Where an answer comes from**, in this order, and `Planned.source` says which:

- `profile`: name, email, phone, city and links, from the newest base resume;
- `file`: the resume itself;
- `saved`: one of the user's saved answers (`answers.plan`), mapped onto the field's options;
- `declined`: a voluntary disclosure nobody answered;
- `skipped`: an optional field with nothing to put in it, left empty;
- `missing`: a required field with nothing to put in it. One of these makes the application
  `needs_answers`, with the question named, until the user answers it.

Nothing is written by a model here, and nothing is sent anywhere: the only request is the GET
that reads the form.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass
from typing import Any, Literal

from django.db import transaction
from django.db.models.functions import Now
from procrastinate.exceptions import AlreadyEnqueued

from apps.applications import answers as saved_answers
from apps.applications import resume
from apps.applications.forms import Form, FormField, FormNotFound, read_form
from apps.applications.models import Application, ApplicationStatus
from apps.core.db import db
from apps.core.http import HttpClient
from apps.core.queue import defer_in_tx
from apps.jobs.models import Job
from apps.matching.models import JobMatch
from apps.resumes import schema
from apps.resumes.queries import latest_base_resume
from apps.runs import step
from apps.runs.events import emit

__all__ = [
    "PREPARE_TASK",
    "PREPARED_EVENT",
    "Planned",
    "Profile",
    "http_client",
    "plan_form",
    "prepare",
    "profile_of",
    "replan",
    "start",
    "start_many",
]

log = logging.getLogger("foray.applications")

PREPARE_TASK = "application.prepare"
# What pages listen for to draw an application that has just been planned.
PREPARED_EVENT = "application.prepared"
# Statuses a preparation may (re)write. One the user sent or dismissed is theirs.
OPEN = (
    ApplicationStatus.PREPARING,
    ApplicationStatus.NEEDS_ANSWERS,
    ApplicationStatus.READY,
    ApplicationStatus.FAILED,
)

type Source = Literal["profile", "file", "saved", "declined", "skipped", "missing"]


def http_client() -> HttpClient:
    """One guarded client per job. Tests replace this to serve canned answers."""
    return HttpClient()


@dataclass(frozen=True, slots=True)
class Planned:
    """One field of a form with the answer planned for it."""

    key: str
    label: str
    type: str
    required: bool
    options: list[str]
    kind: str  # the shared question it asks (`AnswerKind`), or a profile field's name
    value: str | None
    source: Source

    def dump(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Profile:
    """What every form asks and the resume already says."""

    first_name: str = ""
    last_name: str = ""
    full_name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    linkedin: str = ""
    github: str = ""
    website: str = ""


def _link(links: Sequence[schema.Link], host: str) -> str:
    return next((link.url for link in links if host in link.url.lower()), "")


def profile_of(resume: schema.Resume | None) -> Profile:
    if resume is None:
        return Profile()
    basics = resume.basics
    name = " ".join(basics.name.split())
    first, _, last = name.partition(" ")
    others = [
        link.url
        for link in basics.links
        if "linkedin.com" not in link.url.lower() and "github.com" not in link.url.lower()
    ]
    return Profile(
        first_name=first,
        last_name=last.rsplit(" ", 1)[-1] if last else "",
        full_name=name,
        email=basics.email or "",
        phone=basics.phone or "",
        location=basics.location or "",
        linkedin=_link(basics.links, "linkedin.com"),
        github=_link(basics.links, "github.com"),
        website=others[0] if others else "",
    )


# A field that asks for something the profile holds, by its name in the form or by its label.
# The label patterns are anchored: "Preferred first name" and "Emergency contact phone" are
# other questions.
_PROFILE_KEYS = {
    "first_name": "first_name",
    "last_name": "last_name",
    "name": "full_name",
    "full_name": "full_name",
    "email": "email",
    "phone": "phone",
    "location": "location",
}
_PROFILE_LABELS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        ("first_name", r"^\s*(?:legal )?first name\s*\*?\s*$"),
        ("last_name", r"^\s*(?:legal )?(?:last|family) name\s*\*?\s*$"),
        ("full_name", r"^\s*(?:full |legal )?name\s*\*?\s*$"),
        ("email", r"^\s*e-?mail(?: address)?\s*\*?\s*$"),
        ("phone", r"^\s*(?:phone|mobile)(?: number)?\s*\*?\s*$"),
        ("location", r"^\s*(?:current )?location(?: \(city\))?\s*\*?\s*$"),
    )
)
_RESUME = re.compile(r"^\s*(?:resume|cv|resume\s*/\s*cv)\b", re.IGNORECASE)
_COVER = re.compile(r"cover letter", re.IGNORECASE)
# Saved answers the profile can stand in for when the user saved none.
_PROFILE_FOR_KIND = {
    "linkedin": "linkedin",
    "github": "github",
    "website": "website",
    "phone": "phone",
    "location": "location",
}


def _profile_field(field: FormField) -> str | None:
    by_key = _PROFILE_KEYS.get(field.key.lower())
    if by_key is not None:
        return by_key
    return next((name for name, pattern in _PROFILE_LABELS if pattern.match(field.label)), None)


def _plan_field(field: FormField, profile: Profile, saved: saved_answers.SavedAnswers) -> Planned:
    def planned(kind: str, value: str | None, source: Source) -> Planned:
        if value is None and source == "missing" and not field.required:
            source = "skipped"
        return Planned(
            field.key, field.label, field.type, field.required, field.options, kind, value, source
        )

    if field.type == "file":
        if _COVER.search(field.label) or _COVER.search(field.key.replace("_", " ")):
            return planned("cover_letter", None, "missing")
        if _RESUME.match(field.label) or field.key.lower() in ("resume", "cv"):
            return planned("resume", "resume", "file")
        return planned("file", None, "missing")

    name = _profile_field(field)
    if name is not None and getattr(profile, name):
        return planned(name, getattr(profile, name), "profile")

    answer = saved_answers.plan(saved, field.label, field.options)
    if answer.value is None and not field.options:
        stand_in = getattr(profile, _PROFILE_FOR_KIND.get(str(answer.kind), ""), "")
        if stand_in:
            return planned(str(answer.kind), stand_in, "profile")
    source: Source = "missing" if answer.value is None else answer.source  # type: ignore[assignment]
    return planned(str(answer.kind), answer.value, source)


def plan_form(
    fields: Sequence[FormField], profile: Profile, saved: saved_answers.SavedAnswers
) -> list[Planned]:
    """The answer for every field of a form, in the form's order."""
    return [_plan_field(field, profile, saved) for field in fields]


def missing_of(planned: Sequence[Planned] | Sequence[dict[str, Any]]) -> list[str]:
    """The labels of the required fields nothing could be put in."""
    found: list[str] = []
    for item in planned:
        data = item if isinstance(item, dict) else item.dump()
        if data.get("source") == "missing" and data.get("required"):
            found.append(str(data.get("label")))
    return found


# --- starting ------------------------------------------------------------------------------------


def start(user_id: str, match_id: uuid.UUID | str) -> Application | None:
    """This match's application, being prepared: the row and the task that fills it, in one
    transaction. One that exists is prepared again unless the user sent or dismissed it. None
    when the match is not this user's. Sync."""
    with transaction.atomic():
        match = JobMatch.objects.for_user(user_id).filter(id=match_id).first()
        if match is None:
            return None
        application = Application.objects.for_user(user_id).filter(match_id=match.id).first()
        if application is None:
            application = Application.objects.create_for(user_id, match_id=match.id)
        elif application.status not in OPEN:
            return application
        elif application.status != ApplicationStatus.PREPARING:
            Application.objects.for_user(user_id).filter(id=application.id).update(
                status=ApplicationStatus.PREPARING, error="", updated_at=Now()
            )
            application.status = ApplicationStatus.PREPARING
        with suppress(AlreadyEnqueued):  # the job that is already waiting will do
            defer_in_tx(
                step.task_for(PREPARE_TASK),
                queueing_lock=f"prepare:{application.id}",
                application_id=str(application.id),
            )
    return application


def start_many(user_id: str, match_ids: Sequence[uuid.UUID | str]) -> int:
    """Starts a preparation for each match that has no application yet (a watch's kept
    matches). Returns how many were started. Sync."""
    have = set(
        Application.objects.for_user(user_id)
        .filter(match_id__in=list(match_ids))
        .values_list("match_id", flat=True)
    )
    started = 0
    for match_id in match_ids:
        if uuid.UUID(str(match_id)) not in have and start(user_id, match_id) is not None:
            started += 1
    return started


# --- the task ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Subject:
    application: Application
    job: Job
    board_slug: str | None


def _subject(application_id: str) -> _Subject | None:
    application = (
        Application.objects.unscoped("the prepare task finds its application by id")
        .filter(id=application_id, status=ApplicationStatus.PREPARING)
        .select_related("match__job__company")
        .first()
    )
    if application is None:
        return None
    job = application.match.job
    return _Subject(application, job, job.company.ats_slug if job.company else None)


def _status(planned: Sequence[Planned]) -> str:
    return ApplicationStatus.NEEDS_ANSWERS if missing_of(planned) else ApplicationStatus.READY


def _save(application: Application, form: Form, planned: list[Planned]) -> str:
    """Writes what was read and planned, unless the user closed the application meanwhile.
    The event is the transaction's last statement. Sync."""
    status = _status(planned)
    with transaction.atomic():
        updated = (
            Application.objects.for_user(application.user_id)
            .filter(id=application.id, status=ApplicationStatus.PREPARING)
            .update(
                status=status,
                form_source=form.source,
                apply_url=form.apply_url,
                questions=[field.dump() for field in form.fields],
                answers=[item.dump() for item in planned],
                error="",
                prepared_at=Now(),
                ready_at=Now() if status == ApplicationStatus.READY else None,
                updated_at=Now(),
            )
        )
        if updated:
            # Which resume it sends, by the posting's title, and its PDF on the way (#61).
            resume.assign(application.user_id, application)
            resume.ensure_pdf(application.user_id, application)
            emit(
                application.user_id,
                PREPARED_EVENT,
                "An application is ready to send"
                if status == ApplicationStatus.READY
                else "An application needs an answer from you",
                level="debug",
                data={"applicationId": str(application.id), "status": status},
            )
    return status


def _fail(application: Application, reason: str, status: str = ApplicationStatus.FAILED) -> None:
    with transaction.atomic():
        updated = (
            Application.objects.for_user(application.user_id)
            .filter(id=application.id, status=ApplicationStatus.PREPARING)
            .update(status=status, error=reason, updated_at=Now())
        )
        if updated:
            emit(
                application.user_id,
                PREPARED_EVENT,
                reason,
                level="debug",
                data={"applicationId": str(application.id), "status": status},
            )


def _planning_inputs(user_id: str) -> tuple[Profile, saved_answers.SavedAnswers]:
    base = latest_base_resume(user_id)
    return profile_of(base.resume if base else None), saved_answers.load(user_id)


async def prepare(application_id: str) -> str:
    """Reads the posting's form and plans its answers. Returns the status it left, or
    "gone" when there was nothing to prepare."""
    subject = await db(_subject, application_id)
    if subject is None:
        return "gone"
    application, job = subject.application, subject.job
    if not job.is_active:
        await db(_fail, application, "This posting has closed.", ApplicationStatus.DISMISSED)
        return ApplicationStatus.DISMISSED
    try:
        async with http_client() as client:
            form = await read_form(client, job, subject.board_slug)
    except FormNotFound:
        await db(_fail, application, "This posting has closed.", ApplicationStatus.DISMISSED)
        return ApplicationStatus.DISMISSED
    except Exception as error:
        log.warning("reading the form of %s failed: %s", job.id, error)
        await db(_fail, application, "The application form could not be read. Try again later.")
        return ApplicationStatus.FAILED
    profile, saved = await db(_planning_inputs, application.user_id)
    return await db(_save, application, form, plan_form(form.fields, profile, saved))


# --- planning again ------------------------------------------------------------------------------


def replan(user_id: str) -> int:
    """After the user's answers changed: plans every open application again from its stored
    questions. Returns how many became ready. Sync; no fetch."""
    profile, saved = _planning_inputs(user_id)
    became_ready = 0
    rows = Application.objects.for_user(user_id).filter(
        status__in=[ApplicationStatus.NEEDS_ANSWERS, ApplicationStatus.READY]
    )
    for application in rows:
        fields = [field for field in map(FormField.read, application.questions) if field]
        planned = plan_form(fields, profile, saved)
        status = _status(planned)
        dumped = [item.dump() for item in planned]
        if dumped == application.answers and status == application.status:
            continue
        ready_now = (
            status == ApplicationStatus.READY and application.status != ApplicationStatus.READY
        )
        fields_to_set: dict[str, Any] = {"status": status, "answers": dumped, "updated_at": Now()}
        if ready_now:
            fields_to_set["ready_at"] = Now()
        updated = (
            Application.objects.for_user(user_id)
            .filter(id=application.id, status=application.status)
            .update(**fields_to_set)
        )
        became_ready += bool(updated and ready_now)
    return became_ready
