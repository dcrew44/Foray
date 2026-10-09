"""The resume pipelines: reading an upload, tailoring for a match, rendering a PDF (design §9.4,
§11.1, §11.4, §11.6; ports of `src/app/api/uploads/route.ts`, `src/lib/runs/tailor-run.ts` and
`src/worker/handlers/resume.ts`).

Each is a run, so the user sees it on a card and in the feed, and each ends with `run.completed`,
`run.failed` or `run.cancelled` (R4, R17).

**Starting** (sync; the API calls these in its request thread, the chat tools through `db()`):

- `accept_upload(user_id, data, filename, kind)`: hashes first, stores only bytes Foray has not
  seen (a re-upload reuses its document, with no second copy), and writes the document and the
  `profile_import` run in one transaction. If that transaction fails, the file just written is
  deleted, so a failed upload leaves no orphan (bug #30).
- `start_tailor_run(user_id, id, *, conversation_id)`: `id` is a match id or a job id; a job the
  user has no match for yet gets a `found` one. Throttled `tailor:<user>` (§4.5), so the endpoint
  and the chat tool share the count. A second start while one is active returns that run.
- `start_render_run(user_id, resume_id, *, conversation_id)` and `render_base_resume(user_id)`.

**Steps** (async, registered in `tasks.py` under `run_step`, which claims the run, attributes
model calls to it, applies the timeout and turns failures into `run.failed`):

- `import_resume(run)`: reading → extracting → saving. The document's text, the new base
  version (with its `source_document`) and `run.completed` commit together.
- `tailor(run)`: starting → tailoring → verifying → revising → rendering → done. The draft is
  never saved as it is: it is verified against the base resume, unsupported lines are reverted,
  and only then is a tailored version written (with `job_matches.tailored_resume`, in one
  transaction). The PDF comes last and may fail on its own: a warn `render.failed`, and the run
  still completes with `documentId: null`.
- `render(run)`: rendering → done. `resume.render` is the one task that retries (once): a
  rendering error is not a model call, so retrying it spends nothing.

**Stop** is checked between stages (`is_cancelled`), and the writes that save work lock the run
first and save nothing once it is no longer active, so a cancelled run never gains a resume
afterwards. `run.cancelled` itself is emitted only by `lifecycle.cancel()` (§9.9).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from django.db import IntegrityError, transaction

from apps.accounts.models import User
from apps.accounts.schemas import Preferences
from apps.core import throttle
from apps.core.db import db
from apps.core.errors import UserFacingError
from apps.core.storage import StorageError, sha256_hex, storage, storage_key
from apps.jobs.models import Job
from apps.matching import queries as match_queries
from apps.matching.models import JobMatch
from apps.resumes import render as rendering
from apps.resumes.edits import create_base_version
from apps.resumes.extract import ResumeReadError, extract_from, read_document
from apps.resumes.models import Document, DocumentKind, ResumeKind
from apps.resumes.models import Resume as ResumeRow
from apps.resumes.parse.garble import GarbleReport
from apps.resumes.parse.sniff import MIME, DocumentType, UploadRejected, sniff
from apps.resumes.queries import BaseResume, latest_base_resume, latest_base_resume_row
from apps.resumes.schema import Resume, Revision, TailorChange, Verification, with_ids
from apps.resumes.tailor import save_tailored_resume, tailor_resume
from apps.resumes.text import js_trim
from apps.resumes.verify import (
    finalize_verification,
    reconcile_changes,
    revise_tailored,
    verify_tailored,
)
from apps.runs import events, lifecycle
from apps.runs.catalogue import (
    ACTIVE,
    FAILURE_PREFIX,
    EventKind,
    ImportCounts,
    ImportInput,
    ImportOutput,
    Level,
    RenderInput,
    RenderOutput,
    RunKind,
    TailorInput,
    TailorOutput,
)
from apps.runs.models import Run

__all__ = [
    "IMPORT_TASK",
    "READING",
    "RENDER_TASK",
    "TAILOR_TASK",
    "Accepted",
    "Started",
    "accept_upload",
    "done_message",
    "import_resume",
    "read_message",
    "render",
    "render_base_resume",
    "start_render_run",
    "start_tailor_run",
    "tailor",
    "verified_message",
]

log = logging.getLogger("foray.resumes")

# Task names (registered in `tasks.py`; `start_run` resolves them through `step.task_for`).
IMPORT_TASK = "resume.import"
TAILOR_TASK = "resume.tailor"
RENDER_TASK = "resume.render"

# --- Today's sentences (`map/http-surface.md` §3.11, §4.12, §4.13; `map/worker.md` §11) ----------

READING = "Reading your resume…"
READ_PROMPT = "Read your resume"
IMPORT_FAILED = FAILURE_PREFIX[RunKind.PROFILE_IMPORT]  # "Could not read that resume: "

NO_RESUME_TO_TAILOR = "There is no resume on file to tailor. Upload one on your profile first."
JOB_NOT_FOUND = "I couldn't find that job."
NO_JOB_ATTACHED = "This tailoring run has no job attached."
MATCH_GONE = "That job is no longer among your matches."
EMPTY_DRAFT = (
    "The tailored draft came back without any of your experience, so it was discarded. Try again."
)
RENDER_FAILED_IN_TAILOR = (
    "Your tailored resume was saved, but the PDF could not be made. The preview still works; "
    "try rendering it again."
)

NO_RESUME_TO_RENDER = "There is no resume on file yet. Upload one first."
RESUME_GONE = "That resume no longer exists."
RENDERING = "Rendering your resume to PDF. It takes a few seconds."
RENDER_FAILED = FAILURE_PREFIX[RunKind.RESUME_RENDER]  # "Could not render the PDF: "

_DOCUMENT_SHA = "documents_user_sha256"

type Id = str | uuid.UUID


def _as_uuid(value: Id) -> uuid.UUID | None:
    return lifecycle.as_uuid(value)


def _violates(error: IntegrityError, constraint: str) -> bool:
    diag = getattr(error.__cause__, "diag", None)
    return getattr(diag, "constraint_name", None) == constraint


# --- Starting ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Started:
    """What a start answers. `refusal` says why `ok` is False: the API maps it to a status, a
    chat tool shows the message as it is."""

    ok: bool
    message: str
    run_id: uuid.UUID | None = None
    match_id: uuid.UUID | None = None
    refusal: Literal["no_resume", "not_found"] | None = None

    def as_dict(self) -> dict[str, Any]:
        """`{ok, runId, matchId, message}` / `{ok: false, message}`, as the chat tools return."""
        if not self.ok:
            return {"ok": False, "message": self.message}
        out: dict[str, Any] = {"ok": True, "runId": str(self.run_id)}
        if self.match_id is not None:
            out["matchId"] = str(self.match_id)
        out["message"] = self.message
        return out


@dataclass(frozen=True, slots=True)
class Accepted:
    run_id: uuid.UUID
    document_id: uuid.UUID
    new_document: bool  # False: the same bytes were uploaded before, and that document is reused
    new_run: bool  # False: an import of this document was already under way


def _start_import(user_id: str, document: Document, filename: str) -> tuple[Run, bool]:
    return lifecycle.start_run(
        user_id,
        RunKind.PROFILE_IMPORT,
        input=ImportInput(document_id=str(document.id), filename=filename, prompt=READ_PROMPT),
        dedupe_key=f"import:{document.id}",
        task=IMPORT_TASK,
    )


def _restore_file(user_id: str, document: Document, data: bytes) -> None:
    """A reused document whose stored copy is gone gets these (identical) bytes back, under a
    new key; otherwise a re-upload could never repair it. Conditional on the old key, so of two
    concurrent repairs one wins and the other deletes its copy (no orphan)."""
    try:
        if storage().exists(document.storage_path):
            return
    except StorageError:
        pass  # a key the store refuses is as good as missing
    key = storage_key(user_id, document.kind, document.filename)
    storage().put(key, data)
    try:
        repaired = (
            Document.objects.for_user(user_id)
            .filter(id=document.id, storage_path=document.storage_path)
            .update(storage_path=key)
        )
    except BaseException:
        storage().delete(key)
        raise
    if repaired:
        log.warning("document %s: its stored file was missing and is restored", document.id)
        document.storage_path = key
    else:
        storage().delete(key)


def accept_upload(user_id: str, data: bytes, filename: str, kind: DocumentType) -> Accepted:
    """Stores a sniffed upload (if it is new) and starts reading it (§11.1)."""
    digest = sha256_hex(data)
    for attempt in (1, 2):
        existing = Document.objects.for_user(user_id).filter(sha256=digest).first()
        if existing is not None:
            _restore_file(user_id, existing, data)
            run, reused = _start_import(user_id, existing, filename)
            return Accepted(run.id, existing.id, new_document=False, new_run=not reused)

        key = storage_key(user_id, DocumentKind.RESUME_UPLOAD.value, filename)
        storage().put(key, data)
        try:
            with transaction.atomic():
                document = Document.objects.create_for(
                    user_id,
                    kind=DocumentKind.RESUME_UPLOAD,
                    filename=filename,
                    mime=MIME[kind],
                    size_bytes=len(data),
                    storage_path=key,
                    sha256=digest,
                )
                run, reused = _start_import(user_id, document, filename)
        except IntegrityError as error:
            storage().delete(key)  # no orphan
            if attempt == 1 and _violates(error, _DOCUMENT_SHA):
                continue  # the same bytes landed concurrently: reuse that document
            raise
        except BaseException:
            storage().delete(key)  # no orphan
            raise
        return Accepted(run.id, document.id, new_document=True, new_run=not reused)
    raise AssertionError("unreachable")  # pragma: no cover


def _label(job: Job) -> str:
    return f"{job.title} at {job.company_name}"


def start_tailor_run(
    user_id: str, match_or_job_id: str, *, conversation_id: Id | None = None
) -> Started:
    """Starts tailoring for a match (or a job). Counted against `tailor:<user>` first, so the
    endpoint and the chat tool share one count; over the limit it raises `ApiError(429)`."""
    throttle.throttle(f"tailor:{user_id}", throttle.TAILOR.limit, throttle.TAILOR.window_s)
    if latest_base_resume_row(user_id) is None:
        return Started(False, NO_RESUME_TO_TAILOR, refusal="no_resume")
    wanted = js_trim(match_or_job_id)
    # A match id or a job id: a posting the user found themselves gets a `found` match.
    match = match_queries.resolve_or_create_match(user_id, wanted) if wanted else None
    if match is None:
        return Started(False, JOB_NOT_FOUND, refusal="not_found")
    label = _label(match.job)
    run, reused = lifecycle.start_run(
        user_id,
        RunKind.TAILOR,
        input=TailorInput(match_id=str(match.id), prompt=f"Tailor your resume for {label}"),
        conversation_id=conversation_id,
        dedupe_key=f"tailor:{match.id}",
        task=TAILOR_TASK,
    )
    if reused:
        message = f"Already tailoring your resume for {label}. It will be ready in about a minute."
    else:
        message = (
            f"Tailoring your resume for {label}. It takes about a minute, and every change is "
            "checked against your original resume before it is saved."
        )
    return Started(True, message, run_id=run.id, match_id=match.id)


def start_render_run(user_id: str, resume_id: Id, *, conversation_id: Id | None = None) -> Started:
    """Starts rendering a stored resume (base or tailored) to PDF."""
    wanted = _as_uuid(resume_id)
    row = (
        ResumeRow.objects.for_user(user_id).filter(id=wanted).first()
        if wanted is not None
        else None
    )
    if row is None:
        return Started(False, RESUME_GONE, refusal="not_found")
    prompt = (
        "Render your resume to PDF"
        if row.kind == ResumeKind.BASE
        else "Render your tailored resume to PDF"
    )
    run, _ = lifecycle.start_run(
        user_id,
        RunKind.RESUME_RENDER,
        input=RenderInput(resume_id=str(row.id), prompt=prompt),
        conversation_id=conversation_id,
        dedupe_key=f"render:{row.id}",
        task=RENDER_TASK,
    )
    return Started(True, RENDERING, run_id=run.id)


def render_base_resume(user_id: str) -> Started:
    """`POST /api/resume/render`: the newest base version."""
    base = latest_base_resume_row(user_id)
    if base is None:
        return Started(False, NO_RESUME_TO_RENDER, refusal="no_resume")
    return start_render_run(user_id, base.id)


# --- Shared step plumbing -------------------------------------------------------------------------


async def _cancelled(run: Run) -> bool:
    return await db(lifecycle.is_cancelled, run.id)


def _lock_if_active(user_id: str, run_id: Id) -> bool:
    """Inside a transaction: lock the run, and say whether it may still save work. Taken first,
    so a Stop either lands before (and nothing is saved) or waits for the save to commit."""
    return (
        Run.objects.for_user(user_id)
        .select_for_update()
        .filter(id=run_id, status__in=ACTIVE)
        .first()
        is not None
    )


# --- profile_import ------------------------------------------------------------------------------


def read_message(resume: Resume) -> str:
    """Today's toast, now the run's message."""
    return (
        f"Read {len(resume.work)} roles, {len(resume.education)} schools and "
        f"{len(resume.skills)} skills."
    )


def _document(user_id: str, document_id: str) -> Document | None:
    wanted = _as_uuid(document_id)
    if wanted is None:
        return None
    return Document.objects.for_user(user_id).filter(id=wanted).first()


def _read_stored(key: str) -> bytes | None:
    """The stored bytes, or None when the file is gone (or its key is refused): the run says
    so in plain words instead of quoting a path."""
    try:
        with storage().open(key) as handle:
            return handle.read()
    except OSError, StorageError:
        return None


def _save_import(
    run_id: Id,
    user_id: str,
    document_id: uuid.UUID,
    *,
    text: str,
    resume: Resume,
    used_vision: bool,
    garble: GarbleReport,
) -> bool:
    """One transaction: the document's text, the next base version (pointing at the document)
    and `run.completed`. False, with nothing written, when the run was stopped meanwhile."""
    with transaction.atomic():
        if not _lock_if_active(user_id, run_id):
            return False
        Document.objects.for_user(user_id).filter(id=document_id).update(text_extracted=text)
        row = create_base_version(user_id, resume, source_document_id=document_id)
        output = ImportOutput(
            document_id=str(document_id),
            resume_id=str(row.id),
            used_vision=used_vision,
            garble=garble,
            counts=ImportCounts(
                work=len(resume.work),
                education=len(resume.education),
                skills=len(resume.skills),
                projects=len(resume.projects),
            ),
        )
        return lifecycle.complete(run_id, output, read_message(resume))


async def import_resume(run: Run) -> None:
    """`resume.import`: read the uploaded file, extract it, save it as the next base version."""
    spec = ImportInput.read(run.input)
    if spec is None:
        raise UserFacingError(f"{IMPORT_FAILED}This import has no file attached.")
    document = await db(_document, run.user_id, spec.document_id)
    if document is None:
        raise UserFacingError(f"{IMPORT_FAILED}The uploaded file no longer exists.")

    await db(lifecycle.merge_stats, run.id, phase="reading")
    data = await asyncio.to_thread(_read_stored, document.storage_path)
    if data is None:
        raise UserFacingError(f"{IMPORT_FAILED}The file is missing from storage.")
    try:
        kind = sniff(data)  # the stored bytes decide, as they did at upload
        parsed = await read_document(data, kind)
    except UploadRejected as error:
        raise UserFacingError(f"{IMPORT_FAILED}{error.message}") from error
    except ResumeReadError as error:
        raise UserFacingError(f"{IMPORT_FAILED}{error}") from error
    if await _cancelled(run):
        return

    await db(lifecycle.merge_stats, run.id, phase="extracting")
    try:
        extraction = await extract_from(parsed, data, kind)
    except ResumeReadError as error:
        raise UserFacingError(f"{IMPORT_FAILED}{error}") from error
    if await _cancelled(run):
        return

    await db(lifecycle.merge_stats, run.id, phase="saving")
    await db(
        _save_import,
        run.id,
        run.user_id,
        document.id,
        text=parsed.text,
        resume=with_ids(extraction.extracted, "resume"),
        used_vision=extraction.used_vision,
        garble=parsed.garble,
    )


# --- tailor --------------------------------------------------------------------------------------


def verified_message(verification: Verification) -> str:
    """`verifiedMessage`: what the check found, for the `tailor.verified` event."""

    def lines(count: int) -> str:
        return f"{count} {'line' if count == 1 else 'lines'}"

    fixed = len(verification.revisions)
    open_ = len(verification.unsupported)
    parts = [f"Checked {lines(verification.checked)} against your resume."]
    if fixed == 0 and open_ == 0:
        parts.append("Every one is supported by what you wrote.")
    elif fixed > 0:
        parts.append(
            f"{fixed} {'change was' if fixed == 1 else 'changes were'} not backed by your resume "
            f"and {'was' if fixed == 1 else 'were'} put back to your original wording or removed."
        )
    if open_ > 0:
        parts.append(
            f"{lines(open_)} still {'needs' if open_ == 1 else 'need'} your review before you "
            "send it."
        )
    if verification.model_error:
        parts.append(
            "The line-by-line check was unavailable, so rewrites kept your original wording."
        )
    return " ".join(parts)


def done_message(label: str, pages: int | None, unsupported: int) -> str:
    """`doneMessage`: the run's final message."""
    length = (
        " It fits on one page." if pages == 1 else f" It runs to {pages} pages." if pages else ""
    )
    review = (
        f" {unsupported} {'line needs' if unsupported == 1 else 'lines need'} your review before "
        "you send it."
        if unsupported > 0
        else ""
    )
    return f"Your resume for {label} is ready.{length}{review}"


@dataclass(frozen=True, slots=True)
class _Posting:
    """What the tailor reads of a posting (`tailor.Posting`), copied off the row."""

    title: str
    company_name: str
    locations: Sequence[str]
    remote: str
    description_text: str | None
    description_raw: str | None


@dataclass(frozen=True, slots=True)
class _TailorSubject:
    match: JobMatch
    job: _Posting
    base: BaseResume | None
    preferences: Preferences

    @property
    def label(self) -> str:
        return f"{self.job.title} at {self.job.company_name}"


def _tailor_subject(user_id: str, match_id: str) -> _TailorSubject | None:
    wanted = _as_uuid(match_id)
    match = (
        JobMatch.objects.for_user(user_id).select_related("job").filter(id=wanted).first()
        if wanted is not None
        else None
    )
    if match is None:
        return None
    stored = User.objects.filter(pk=user_id).values_list("preferences", flat=True).first()
    job = match.job
    return _TailorSubject(
        match=match,
        job=_Posting(
            title=job.title,
            company_name=job.company_name,
            locations=list(job.locations or []),
            remote=job.remote,
            description_text=job.description_text,
            description_raw=job.description_raw,
        ),
        base=latest_base_resume(user_id),
        preferences=Preferences.read(stored),
    )


def _tailoring(run: Run, subject: _TailorSubject, base: BaseResume) -> None:
    with transaction.atomic():
        lifecycle.merge_stats(
            run.id,
            phase="tailoring",
            matchId=str(subject.match.id),
            title=subject.job.title,
            company=subject.job.company_name,
            baseVersion=base.version,
        )
        events.emit(
            run.user_id,
            EventKind.TAILOR_STARTED,
            f"Tailoring your resume for {subject.label}.",
            run_id=run.id,
            data={"matchId": str(subject.match.id)},
        )


def _save_tailored(
    run: Run,
    match_id: uuid.UUID,
    *,
    resume: Resume,
    changes: list[TailorChange],
    verification: Verification,
    revisions: list[Revision],
) -> uuid.UUID | None:
    """One transaction: the tailored version, the match's pointer, the stats and
    `tailor.verified`. None, with nothing written, when the run was stopped meanwhile."""
    with transaction.atomic():
        if not _lock_if_active(run.user_id, run.id):
            return None
        saved = save_tailored_resume(
            run.user_id, match_id, resume=resume, changes=changes, verification=verification
        )
        unsupported = len(verification.unsupported)
        lifecycle.merge_stats(
            run.id,
            resumeId=str(saved.id),
            checked=verification.checked,
            revisedCount=len(revisions),
            unsupported=unsupported,
            changes=len(changes),
        )
        events.emit(
            run.user_id,
            EventKind.TAILOR_VERIFIED,
            verified_message(verification),
            run_id=run.id,
            level=Level.INFO if verification.passed else Level.WARN,
            data={
                "resumeId": str(saved.id),
                "checked": verification.checked,
                "modelChecked": verification.model_checked,
                "revised": len(revisions),
                "unsupported": unsupported,
                "passed": verification.passed,
            },
        )
        return saved.id


async def tailor(run: Run) -> None:
    """`resume.tailor`: draft, verify, revise, save, then render (`executeTailorRun` and
    `handleResumeTailor`)."""
    await db(lifecycle.merge_stats, run.id, phase="starting")
    spec = TailorInput.read(run.input)
    if spec is None or not spec.match_id:
        raise UserFacingError(NO_JOB_ATTACHED)
    subject = await db(_tailor_subject, run.user_id, spec.match_id)
    if subject is None:
        raise UserFacingError(MATCH_GONE)
    base = subject.base
    if base is None:
        raise UserFacingError(NO_RESUME_TO_TAILOR)
    label = subject.label
    await db(_tailoring, run, subject, base)

    # 1. The draft.
    draft = await tailor_resume(
        base.resume,
        subject.job,
        deep_eval=subject.match.deep_eval,
        fast_eval=subject.match.fast_eval,
        preferences=subject.preferences,
    )
    if not draft.resume.work and base.resume.work:
        raise UserFacingError(EMPTY_DRAFT)
    if await _cancelled(run):
        return

    # 2. Every line against the base resume.
    await db(lifecycle.merge_stats, run.id, phase="verifying")
    result = await verify_tailored(base.resume, draft.resume, draft.changes)
    # What the mapping refused (an item or skill not on the base) is already gone; it belongs
    # in the record all the same.
    revisions = [drop.as_revision() for drop in draft.dropped]

    # 3. One deterministic revision of whatever failed.
    final = draft.resume
    if result.findings:
        await db(lifecycle.merge_stats, run.id, phase="revising")
        final, revised = revise_tailored(base.resume, draft.resume, result.findings)
        revisions.extend(revised)

    verification = finalize_verification(
        base=base.resume,
        final=final,
        result=result,
        revisions=revisions,
        base_resume_id=str(base.id),
        base_version=base.version,
    )
    changes = reconcile_changes(
        base=base.resume, final=final, changes=draft.changes, revisions=revisions
    )
    if await _cancelled(run):
        return

    # 4. Save, and make it the match's current tailored resume.
    resume_id = await db(
        _save_tailored,
        run,
        subject.match.id,
        resume=final,
        changes=changes,
        verification=verification,
        revisions=revisions,
    )
    if resume_id is None:
        return  # stopped while verifying: nothing saved

    # 5. The PDF, which may fail on its own: the verified resume is already saved.
    await db(lifecycle.merge_stats, run.id, phase="rendering")
    rendered: rendering.RenderResult | None = None
    try:
        rendered = await rendering.render_resume(run.user_id, resume_id)
    except Exception:
        log.exception("tailor run %s: the PDF could not be rendered", run.id)
        await db(
            events.emit,
            run.user_id,
            EventKind.RENDER_FAILED,
            RENDER_FAILED_IN_TAILOR,
            run_id=run.id,
            level=Level.WARN,
            data={"resumeId": str(resume_id)},
        )

    unsupported = len(verification.unsupported)
    document_id = str(rendered.document_id) if rendered else None
    pages = rendered.pages if rendered else None
    density = rendered.density if rendered else None
    await db(
        lifecycle.complete,
        run.id,
        TailorOutput(
            resume_id=str(resume_id),
            document_id=document_id,
            pages=pages,
            changes=len(changes),
            unsupported=unsupported,
            match_id=str(subject.match.id),
            density=density,
        ),
        done_message(label, pages, unsupported),
        level=Level.WARN if unsupported > 0 else Level.INFO,
        stats={"documentId": document_id, "pages": pages, "density": density},
    )


# --- resume_render -------------------------------------------------------------------------------


async def render(run: Run) -> None:
    """`resume.render`: a stored resume to a stored PDF, reused when nothing changed."""
    spec = RenderInput.read(run.input)
    if spec is None:
        raise UserFacingError(f"{RENDER_FAILED}This render has no resume attached.")
    await db(lifecycle.merge_stats, run.id, phase="rendering")
    try:
        result = await rendering.render_resume(run.user_id, spec.resume_id)
    except rendering.RenderError as error:
        # The resume is gone or unreadable: retrying cannot help.
        raise UserFacingError(f"{RENDER_FAILED}{error}") from error
    await db(
        lifecycle.complete,
        run.id,
        RenderOutput(
            resume_id=spec.resume_id,
            document_id=str(result.document_id),
            pages=result.pages,
            density=result.density,
        ),
        f"Rendered your resume to a {result.pages}-page PDF.",
        stats={
            "documentId": str(result.document_id),
            "pages": result.pages,
            "density": result.density,
        },
    )
