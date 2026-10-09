"""The resume pipelines, end to end on the real job table (design §9.4, §11.1, §11.4, §11.6,
§11.8): an upload read by a `profile_import` run, tailoring a match, rendering a PDF, the
preview, and the resume editor's save.

Runs are driven by `run_deferred()`, a real procrastinate Worker in-process (the worker command
is never started), with fake models (`tests/fakes.py` over `apps/resumes/fakes.py`) behind the
real budget, limiter and ledger wrappers. Rendering is faked except in the `slow` test, which
renders with a real Chromium.
"""

from __future__ import annotations

import asyncio
import io
import json
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from playwright.async_api import Error as PlaywrightError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel

from apps.accounts.models import User
from apps.core import throttle
from apps.core.db import db
from apps.core.storage import sha256_hex, storage
from apps.llm.models import LlmCall
from apps.matching import queries as match_queries
from apps.matching.models import JobMatch
from apps.resumes import fakes as resume_fakes
from apps.resumes import pipeline
from apps.resumes import render as rendering
from apps.resumes.models import Document, DocumentKind, ResumeKind
from apps.resumes.models import Resume as ResumeRow
from apps.resumes.parse.pdf import parse_pdf
from apps.resumes.render import RenderError, RenderResult
from apps.resumes.schema import Resume, Verification
from apps.runs import lifecycle
from apps.runs.models import Event, Run
from tests.factories import (
    make_job,
    make_match,
    make_resume,
    make_run,
    make_user,
    sample_resume_data,
)
from tests.fakes import fake_models
from tests.queue import arun_deferred, deferred, run_deferred

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "resumes"
ORIGIN = "http://localhost:3000"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def blank_pdf() -> bytes:
    """A one-page PDF with no text layer at all: what a scan looks like to the garble check."""
    document = pdfium.PdfDocument.new()
    try:
        document.new_page(612, 792).close()
        buffer = io.BytesIO()
        document.save(buffer)
        return buffer.getvalue()
    finally:
        document.close()


class Browser:
    """A signed-in client: JSON bodies and multipart uploads, with the browser's Origin."""

    def __init__(self, user: User) -> None:
        self.user = user
        self.client = Client()
        self.client.force_login(user)

    def post(self, path: str, body: Any = None) -> Any:
        payload = body if isinstance(body, str) else json.dumps({} if body is None else body)
        return self.client.post(
            path, data=payload, content_type="application/json", headers={"Origin": ORIGIN}
        )

    def put(self, path: str, body: Any) -> Any:
        payload = body if isinstance(body, str) else json.dumps(body)
        return self.client.put(
            path, data=payload, content_type="application/json", headers={"Origin": ORIGIN}
        )

    def upload(self, data: bytes, name: str = "resume.pdf") -> Any:
        return self.client.post(
            "/api/uploads",
            {"file": SimpleUploadedFile(name, data, content_type="application/pdf")},
            headers={"Origin": ORIGIN},
        )

    def get(self, path: str) -> Any:
        return self.client.get(path)


@pytest.fixture(autouse=True)
def _storage_dir(settings: Any, tmp_path: Path) -> Path:
    settings.STORAGE_DIR = str(tmp_path / "storage")
    return tmp_path / "storage"


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The resume lane's own fakes for `extract`, `tailor` and `verify`, in the real stack."""
    fake_models(
        monkeypatch,
        {
            "extract": answering(resume_fakes.extract_resume),
            "tailor": answering(resume_fakes.tailor),
            "verify": answering(resume_fakes.verify),
        },
    )


def _pdf_document(user_id: str, resume_id: str) -> Document:
    data = f"%PDF-1.7 rendered {resume_id} {uuid.uuid4().hex}".encode()
    key = storage().put(f"{user_id}/resume_pdf/1-Resume.pdf", data)
    document = Document.objects.create_for(
        user_id,
        kind=DocumentKind.RESUME_PDF,
        filename="Resume.pdf",
        mime="application/pdf",
        size_bytes=len(data),
        storage_path=key,
        sha256=sha256_hex(data),
    )
    ResumeRow.objects.for_user(user_id).filter(id=resume_id).update(
        rendered_document_id=document.id
    )
    return document


@pytest.fixture
def fake_render(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """`render.render_resume` without a browser: a stored one-page "PDF" per call. The list
    collects the resume ids it was asked for."""
    calls: list[str] = []

    async def render_resume(user_id: str, resume_id: uuid.UUID | str) -> RenderResult:
        calls.append(str(resume_id))
        document = await db(_pdf_document, user_id, str(resume_id))
        return RenderResult(document_id=document.id, pages=1, density="normal", reused=False)

    monkeypatch.setattr(rendering, "render_resume", render_resume)
    return calls


def kinds(run: Run) -> list[str]:
    return list(
        Event.objects.for_user(run.user_id).filter(run_id=run.id).order_by("id")
        .values_list("kind", flat=True)
    )  # fmt: skip


def events_of(run: Run) -> list[Event]:
    return list(Event.objects.for_user(run.user_id).filter(run_id=run.id).order_by("id"))


def reload(run_id: str | uuid.UUID, user: User) -> Run:
    return Run.objects.for_user(user.pk).get(id=run_id)


def out(run: Run) -> dict[str, Any]:
    assert isinstance(run.output, dict), run.output
    return run.output


def stats_of(run: Run) -> dict[str, Any]:
    assert isinstance(run.stats, dict), run.stats
    return run.stats


type FakeFunction = Callable[[list[ModelMessage], AgentInfo], ModelResponse | None]


def answering(function: FakeFunction) -> FunctionModel:
    """A `FunctionModel` over one of the lane's fakes, which answer None for a request that is
    not theirs (here, that would be a test bug)."""

    def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        response = function(messages, info)
        assert response is not None, "the fake declined this request"
        return response

    return FunctionModel(answer)


# --- profile_import ------------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_upload_is_read_by_an_import_run(fakes: None) -> None:
    user = make_user(with_resume=True)  # version 1 exists: the upload becomes version 2
    browser = Browser(user)
    data = fixture("two-column.pdf")
    accepted = browser.upload(data, "two-column.pdf")
    assert accepted.status_code == 202, accepted.content
    body = accepted.json()

    run_deferred()

    run = reload(body["runId"], user)
    assert run.status == "completed", run.error
    resume_row = ResumeRow.objects.for_user(user.pk).get(kind=ResumeKind.BASE, version=2)
    resume = Resume.model_validate(resume_row.data)
    assert str(resume_row.source_document_id) == body["documentId"]
    assert resume.work and resume.education  # the fake reads the text with line heuristics
    assert {item.source for item in resume.work} == {"resume"}
    counts = {
        "work": len(resume.work),
        "education": len(resume.education),
        "skills": len(resume.skills),
        "projects": len(resume.projects),
    }
    assert run.output == {
        "documentId": body["documentId"],
        "resumeId": str(resume_row.id),
        "usedVision": False,
        "garble": out(run)["garble"],
        "counts": counts,
    }
    assert out(run)["garble"]["garbled"] is False
    assert stats_of(run)["phase"] == "done"

    [final] = events_of(run)
    assert final.kind == "run.completed"
    assert final.message == (
        f"Read {counts['work']} roles, {counts['education']} schools and {counts['skills']} skills."
    )
    assert final.data == run.output

    document = Document.objects.for_user(user.pk).get(id=body["documentId"])
    assert document.text_extracted == parse_pdf(data).text
    # The one model call is the run's.
    [call] = LlmCall.objects.for_user(user.pk).filter(run_id=run.id)
    assert call.purpose == "extract"


@pytest.mark.django_db(transaction=True)
def test_import_reads_a_word_file(fakes: None) -> None:
    user = make_user()
    body = Browser(user).upload(fixture("plain.docx"), "plain.docx").json()
    run_deferred()
    run = reload(body["runId"], user)
    assert run.status == "completed", run.error
    assert out(run)["usedVision"] is False
    assert out(run)["counts"]["work"] == 3
    assert ResumeRow.objects.for_user(user.pk).get().version == 1


@pytest.mark.django_db(transaction=True)
def test_import_reads_page_images_when_the_text_layer_is_unusable(fakes: None) -> None:
    user = make_user()
    body = Browser(user).upload(blank_pdf(), "scan.pdf").json()
    run_deferred()
    run = reload(body["runId"], user)
    assert run.status == "completed", run.error
    assert out(run)["usedVision"] is True
    assert out(run)["garble"]["garbled"] is True
    assert out(run)["garble"]["reasons"][0].startswith("only 0 characters per page")


@pytest.mark.django_db(transaction=True)
def test_import_failure_is_plain_words(fakes: None) -> None:
    user = make_user()
    body = Browser(user).upload(b"%PDF-1.7\nnot really a pdf", "broken.pdf").json()
    run_deferred()
    run = reload(body["runId"], user)
    assert run.status == "failed"
    assert run.error == "Could not read that resume: Invalid PDF structure."
    [final] = events_of(run)
    assert (final.kind, final.message, final.level) == ("run.failed", run.error, "error")
    assert not ResumeRow.objects.for_user(user.pk).exists()
    assert Document.objects.for_user(user.pk).get().text_extracted is None
    assert not LlmCall.objects.for_user(user.pk).exists()  # failed before any model call


@pytest.mark.django_db(transaction=True)
def test_import_of_a_missing_file_says_so(fakes: None) -> None:
    user = make_user()
    body = Browser(user).upload(fixture("single-column.pdf")).json()
    storage().delete(Document.objects.for_user(user.pk).get().storage_path)
    run_deferred()
    run = reload(body["runId"], user)
    assert (run.status, run.error) == (
        "failed",
        "Could not read that resume: The file is missing from storage.",
    )
    assert stats_of(run)["phase"] == "reading"


@pytest.mark.django_db(transaction=True)
def test_import_model_failure_fails_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise RuntimeError("the provider is down")

    fake_models(monkeypatch, {"extract": FunctionModel(broken)})
    user = make_user()
    body = Browser(user).upload(fixture("single-column.pdf")).json()
    run_deferred()
    run = reload(body["runId"], user)
    assert run.status == "failed"
    assert run.error == "Could not read that resume: the provider is down"
    assert kinds(run) == ["run.failed"]
    # The failed request is ledgered against the run (R10).
    assert LlmCall.objects.for_user(user.pk).filter(run_id=run.id).count() == 1


@pytest.mark.django_db(transaction=True)
def test_import_stopped_mid_run_saves_nothing(fakes: None, monkeypatch: pytest.MonkeyPatch) -> None:
    user = make_user()
    real_extract = pipeline.extract_from

    async def extract_then_stop(*args: Any, **kwargs: Any) -> Any:
        result = await real_extract(*args, **kwargs)
        run = await db(lambda: Run.objects.for_user(user.pk).get())
        assert await db(lifecycle.cancel, user.pk, run.id)
        return result

    monkeypatch.setattr(pipeline, "extract_from", extract_then_stop)
    body = Browser(user).upload(fixture("single-column.pdf")).json()
    run_deferred()
    run = reload(body["runId"], user)
    assert run.status == "cancelled"
    assert kinds(run) == ["run.cancelled"]  # emitted once, by cancel(), and last
    assert not ResumeRow.objects.for_user(user.pk).exists()
    assert Document.objects.for_user(user.pk).get().text_extracted is None


@pytest.mark.django_db
def test_import_save_refuses_a_finished_run() -> None:
    """The save locks the run first: a run stopped meanwhile gains no resume."""
    user = make_user()
    run = make_run(user, kind="profile_import", status="cancelled")
    document = Document.objects.create_for(
        user.pk,
        kind=DocumentKind.RESUME_UPLOAD,
        filename="r.pdf",
        mime="application/pdf",
        size_bytes=1,
        storage_path=f"{user.pk}/resume_upload/1-r.pdf",
        sha256="0" * 64,
    )
    saved = pipeline._save_import(  # pyright: ignore[reportPrivateUsage]
        run.id,
        user.pk,
        document.id,
        text="text",
        resume=Resume.model_validate(sample_resume_data()),
        used_vision=False,
        garble=parse_pdf(fixture("single-column.pdf")).garble,
    )
    assert saved is False
    assert not ResumeRow.objects.for_user(user.pk).exists()
    assert Document.objects.for_user(user.pk).get().text_extracted is None


# --- tailor --------------------------------------------------------------------------------------


@pytest.fixture
def tailor_setup() -> Callable[..., tuple[Browser, JobMatch]]:
    def make(**match_fields: Any) -> tuple[Browser, JobMatch]:
        user = make_user(with_resume=True)
        job = make_job(title="Backend Engineer", company=None)
        match = make_match(user, job=job, fast_score=74, deep_score=82, **match_fields)
        return Browser(user), match

    return make


@pytest.mark.django_db(transaction=True)
def test_tailor_end_to_end(
    fakes: None, fake_render: list[str], tailor_setup: Callable[..., tuple[Browser, JobMatch]]
) -> None:
    browser, match = tailor_setup()
    label = f"Backend Engineer at {match.job.company_name}"
    started = browser.post("/api/resumes/tailor", {"matchId": str(match.id)})
    assert started.status_code == 202, started.content
    body = started.json()
    assert body == {
        "ok": True,
        "runId": body["runId"],
        "matchId": str(match.id),
        "message": (
            f"Tailoring your resume for {label}. It takes about a minute, and every change is "
            "checked against your original resume before it is saved."
        ),
    }
    [job] = deferred("resume.tailor")
    assert (job.queue, job.lock, job.queueing_lock) == (
        "interactive",
        f"run:{body['runId']}",
        f"run:{body['runId']}",
    )
    run = reload(body["runId"], browser.user)
    assert run.input == {"matchId": str(match.id), "prompt": f"Tailor your resume for {label}"}
    assert run.dedupe_key == f"tailor:{match.id}"

    run_deferred()

    run = reload(body["runId"], browser.user)
    assert run.status == "completed", run.error
    tailored = ResumeRow.objects.for_user(browser.user.pk).get(kind=ResumeKind.TAILORED)
    assert (tailored.match_id, tailored.version) == (match.id, 1)
    assert JobMatch.objects.for_user(browser.user.pk).get(id=match.id).tailored_resume_id == (
        tailored.id
    )
    verification = Verification.model_validate(tailored.verification)
    assert verification.passed is True
    assert verification.base_version == 1
    assert fake_render == [str(tailored.id)]
    document_id = out(run)["documentId"]
    assert run.output == {
        "resumeId": str(tailored.id),
        "documentId": document_id,
        "pages": 1,
        "changes": 0,
        "unsupported": 0,
        "matchId": str(match.id),
        "density": "normal",
    }
    stats = stats_of(run)
    assert stats["phase"] == "done"
    assert (stats["matchId"], stats["title"], stats["baseVersion"]) == (
        str(match.id),
        "Backend Engineer",
        1,
    )
    assert (stats["resumeId"], stats["documentId"], stats["pages"]) == (
        str(tailored.id),
        document_id,
        1,
    )

    started_event, verified, final = events_of(run)
    assert (started_event.kind, started_event.message) == (
        "tailor.started",
        f"Tailoring your resume for {label}.",
    )
    assert started_event.data == {"matchId": str(match.id)}
    assert verified.kind == "tailor.verified" and verified.level == "info"
    assert verified.message == (
        f"Checked {verification.checked} lines against your resume. Every one is supported by "
        "what you wrote."
    )
    assert verified.data == {
        "resumeId": str(tailored.id),
        "checked": verification.checked,
        "modelChecked": 0,
        "revised": 0,
        "unsupported": 0,
        "passed": True,
    }
    assert (final.kind, final.level, final.message) == (
        "run.completed",
        "info",
        f"Your resume for {label} is ready. It fits on one page.",
    )
    # The tailor call is the run's; the fake tailor rewrites nothing, so no judge call.
    purposes = list(
        LlmCall.objects.for_user(browser.user.pk).filter(run_id=run.id)
        .values_list("purpose", flat=True)
    )  # fmt: skip
    assert purposes == ["tailor"]

    # The preview of what was saved keeps its CSP (§11.8).
    preview = browser.get(f"/api/resumes/{tailored.id}/preview")
    assert preview.status_code == 200
    assert preview["Content-Security-Policy"] == (
        "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'self'"
    )


@pytest.mark.django_db
def test_tailor_accepts_a_job_id_and_creates_the_match(
    tailor_setup: Callable[..., tuple[Browser, JobMatch]],
) -> None:
    browser, _ = tailor_setup()
    job = make_job(title="Platform Engineer")
    response = browser.post("/api/resumes/tailor", {"matchId": f"  {job.id} "})
    assert response.status_code == 202, response.content
    match = JobMatch.objects.for_user(browser.user.pk).get(job=job)
    assert match.stage == "found"
    assert response.json()["matchId"] == str(match.id)
    # The job id again finds the same match.
    again = browser.post("/api/resumes/tailor", {"matchId": job.id}).json()
    assert again["matchId"] == str(match.id)
    assert again["runId"] == response.json()["runId"]


@pytest.mark.django_db
def test_tailoring_resolves_ids_through_the_one_match_resolver(
    tailor_setup: Callable[..., tuple[Browser, JobMatch]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`apps.matching.queries.resolve_or_create_match` is the one place a match id or a job id
    becomes a match: the tailor start uses it, and keeps no copy of its own."""
    browser, match = tailor_setup()
    asked: list[str] = []
    resolve = match_queries.resolve_or_create_match

    def spy(user_id: str, wanted: str) -> JobMatch | None:
        asked.append(wanted)
        return resolve(user_id, wanted)

    monkeypatch.setattr(match_queries, "resolve_or_create_match", spy)
    other_job = make_job(title="Data Engineer")
    by_match = pipeline.start_tailor_run(browser.user.pk, str(match.id))
    by_job = pipeline.start_tailor_run(browser.user.pk, f" {other_job.id} ")
    assert asked == [str(match.id), other_job.id]  # trimmed before it is resolved
    assert by_match.match_id == match.id
    created = JobMatch.objects.for_user(browser.user.pk).get(job=other_job)
    assert (by_job.match_id, created.stage) == (created.id, "found")
    assert not hasattr(pipeline, "resolve_match")


@pytest.mark.django_db
def test_tailor_twice_returns_the_active_run(
    tailor_setup: Callable[..., tuple[Browser, JobMatch]],
) -> None:
    browser, match = tailor_setup()
    first = browser.post("/api/resumes/tailor", {"matchId": str(match.id)}).json()
    second = browser.post("/api/resumes/tailor", {"matchId": str(match.id)})
    assert second.status_code == 202
    assert second.json()["runId"] == first["runId"]
    assert second.json()["message"] == (
        f"Already tailoring your resume for Backend Engineer at {match.job.company_name}. "
        "It will be ready in about a minute."
    )
    assert len(deferred("resume.tailor")) == 1


@pytest.mark.django_db
def test_tailor_refusals(tailor_setup: Callable[..., tuple[Browser, JobMatch]]) -> None:
    browser, match = tailor_setup()
    no_job = {"ok": False, "message": "No job was given to tailor for."}
    for body in ({"matchId": "   "}, {"matchId": 5}, {}, "not json"):
        response = browser.post("/api/resumes/tailor", body)
        assert response.status_code == 400, body
        if body != "not json":
            assert response.json() == no_job, body

    unknown = browser.post("/api/resumes/tailor", {"matchId": str(uuid.uuid4())})
    assert (unknown.status_code, unknown.json()) == (
        404,
        {"ok": False, "message": "I couldn't find that job."},
    )
    _, others_match = tailor_setup()
    foreign = browser.post("/api/resumes/tailor", {"matchId": str(others_match.id)})
    assert foreign.status_code == 404

    no_resume = Browser(make_user())
    refused = no_resume.post("/api/resumes/tailor", {"matchId": match.job_id})
    assert (refused.status_code, refused.json()["message"]) == (
        422,
        "There is no resume on file to tailor. Upload one on your profile first.",
    )
    assert not Run.objects.for_user(no_resume.user.pk).exists()


@pytest.mark.django_db
def test_tailor_throttle(tailor_setup: Callable[..., tuple[Browser, JobMatch]]) -> None:
    browser, match = tailor_setup()
    limit = throttle.TAILOR
    for _ in range(limit.limit):
        throttle.throttle(f"tailor:{browser.user.pk}", limit.limit, limit.window_s)
    response = browser.post("/api/resumes/tailor", {"matchId": str(match.id)})
    assert (response.status_code, response.json()["message"]) == (
        429,
        "Too many attempts. Try again in a few minutes.",
    )
    assert not Run.objects.for_user(browser.user.pk).exists()


@pytest.mark.django_db
def test_started_as_dict_is_the_chat_tool_shape(
    tailor_setup: Callable[..., tuple[Browser, JobMatch]],
) -> None:
    browser, match = tailor_setup()
    started = pipeline.start_tailor_run(browser.user.pk, str(match.id), conversation_id=None)
    assert started.as_dict() == {
        "ok": True,
        "runId": str(started.run_id),
        "matchId": str(match.id),
        "message": started.message,
    }
    refused = pipeline.start_tailor_run(browser.user.pk, "nope")
    assert refused.as_dict() == {"ok": False, "message": "I couldn't find that job."}


@pytest.mark.django_db(transaction=True)
def test_tailor_render_failure_still_completes(
    fakes: None,
    monkeypatch: pytest.MonkeyPatch,
    tailor_setup: Callable[..., tuple[Browser, JobMatch]],
) -> None:
    async def broken_render(user_id: str, resume_id: Any) -> RenderResult:
        raise RuntimeError("Chromium went away")

    monkeypatch.setattr(rendering, "render_resume", broken_render)
    browser, match = tailor_setup()
    body = browser.post("/api/resumes/tailor", {"matchId": str(match.id)}).json()
    run_deferred()
    run = reload(body["runId"], browser.user)
    assert run.status == "completed", run.error
    assert (out(run)["documentId"], out(run)["pages"]) == (None, None)
    assert kinds(run) == ["tailor.started", "tailor.verified", "render.failed", "run.completed"]
    render_failed, final = events_of(run)[2:]
    assert render_failed.level == "warn"
    assert render_failed.message == (
        "Your tailored resume was saved, but the PDF could not be made. The preview still "
        "works; try rendering it again."
    )
    assert render_failed.data == {"resumeId": out(run)["resumeId"]}
    assert (
        final.message == f"Your resume for Backend Engineer at {match.job.company_name} is ready."
    )


@pytest.mark.django_db(transaction=True)
def test_tailor_model_failure_fails_the_run(
    fakes: None,
    fake_render: list[str],
    monkeypatch: pytest.MonkeyPatch,
    tailor_setup: Callable[..., tuple[Browser, JobMatch]],
) -> None:
    def broken(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise RuntimeError("Fake failure")

    fake_models(monkeypatch, {"tailor": FunctionModel(broken)})
    browser, match = tailor_setup()
    body = browser.post("/api/resumes/tailor", {"matchId": str(match.id)}).json()
    run_deferred()
    run = reload(body["runId"], browser.user)
    assert run.status == "failed"
    assert run.error == "Tailoring failed: Fake failure"
    assert kinds(run) == ["tailor.started", "run.failed"]
    assert not ResumeRow.objects.for_user(browser.user.pk).filter(kind="tailored").exists()
    assert fake_render == []


@pytest.mark.django_db(transaction=True)
def test_tailor_draft_without_experience_is_discarded(
    fakes: None,
    fake_render: list[str],
    monkeypatch: pytest.MonkeyPatch,
    tailor_setup: Callable[..., tuple[Browser, JobMatch]],
) -> None:
    def no_work(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse | None:
        response = resume_fakes.tailor(messages, info)
        assert response is not None
        call = response.parts[0]
        args = call.args_as_dict()  # pyright: ignore[reportAttributeAccessIssue]
        call.args = {**args, "work": []}  # pyright: ignore[reportAttributeAccessIssue]
        return response

    fake_models(monkeypatch, {"tailor": answering(no_work)})
    browser, match = tailor_setup()
    body = browser.post("/api/resumes/tailor", {"matchId": str(match.id)}).json()
    run_deferred()
    run = reload(body["runId"], browser.user)
    assert run.status == "failed"
    assert run.error == pipeline.EMPTY_DRAFT


@pytest.mark.django_db(transaction=True)
def test_tailor_stopped_mid_run_saves_nothing(
    fakes: None,
    fake_render: list[str],
    monkeypatch: pytest.MonkeyPatch,
    tailor_setup: Callable[..., tuple[Browser, JobMatch]],
) -> None:
    browser, match = tailor_setup()
    real_verify = pipeline.verify_tailored

    async def verify_then_stop(*args: Any, **kwargs: Any) -> Any:
        result = await real_verify(*args, **kwargs)
        run = await db(lambda: Run.objects.for_user(browser.user.pk).get())
        assert await db(lifecycle.cancel, browser.user.pk, run.id)
        return result

    monkeypatch.setattr(pipeline, "verify_tailored", verify_then_stop)
    body = browser.post("/api/resumes/tailor", {"matchId": str(match.id)}).json()
    run_deferred()
    run = reload(body["runId"], browser.user)
    assert run.status == "cancelled"
    assert kinds(run)[-1] == "run.cancelled"
    assert [k for k in kinds(run) if k.startswith("run.")] == ["run.cancelled"]
    assert not ResumeRow.objects.for_user(browser.user.pk).filter(kind="tailored").exists()
    assert JobMatch.objects.for_user(browser.user.pk).get(id=match.id).tailored_resume_id is None
    assert fake_render == []


@pytest.mark.django_db(transaction=True)
def test_tailor_of_a_vanished_match_fails_plainly(
    fakes: None, tailor_setup: Callable[..., tuple[Browser, JobMatch]]
) -> None:
    browser, match = tailor_setup()
    body = browser.post("/api/resumes/tailor", {"matchId": str(match.id)}).json()
    JobMatch.objects.for_user(browser.user.pk).filter(id=match.id).delete()
    run_deferred()
    run = reload(body["runId"], browser.user)
    assert (run.status, run.error) == ("failed", "That job is no longer among your matches.")
    assert stats_of(run)["phase"] == "starting"  # written on claim


def test_messages() -> None:
    def verification(**fields: Any) -> Verification:
        base: dict[str, Any] = {
            "passed": True,
            "checkedAt": "2026-10-08T00:00:00.000Z",
            "unsupported": [],
            "revised": False,
            "checked": 1,
            "modelChecked": 0,
            "revisions": [],
            "modelError": None,
            "baseResumeId": None,
            "baseVersion": None,
        }
        return Verification.model_validate({**base, **fields})

    revision = {
        "section": "work",
        "item_id": "w1",
        "text": "x",
        "reason": "y",
        "source": "rule",
        "action": "reverted",
        "replacement": None,
    }
    finding = {"section": "work", "item_id": "w1", "text": "x", "reason": "y", "source": "rule"}
    assert pipeline.verified_message(verification()) == (
        "Checked 1 line against your resume. Every one is supported by what you wrote."
    )
    assert pipeline.verified_message(
        verification(checked=12, revisions=[revision], modelError="timeout")
    ) == (
        "Checked 12 lines against your resume. 1 change was not backed by your resume and was "
        "put back to your original wording or removed. The line-by-line check was unavailable, "
        "so rewrites kept your original wording."
    )
    assert pipeline.verified_message(
        verification(checked=9, revisions=[revision, revision], unsupported=[finding, finding])
    ) == (
        "Checked 9 lines against your resume. 2 changes were not backed by your resume and were "
        "put back to your original wording or removed. 2 lines still need your review before "
        "you send it."
    )
    assert pipeline.verified_message(verification(checked=3, unsupported=[finding])) == (
        "Checked 3 lines against your resume. 1 line still needs your review before you send it."
    )
    assert (
        pipeline.done_message("X at Y", 1, 0)
        == "Your resume for X at Y is ready. It fits on one page."
    )
    assert pipeline.done_message("X at Y", 2, 1) == (
        "Your resume for X at Y is ready. It runs to 2 pages. 1 line needs your review before "
        "you send it."
    )
    assert pipeline.done_message("X at Y", None, 3) == (
        "Your resume for X at Y is ready. 3 lines need your review before you send it."
    )


# --- resume_render -------------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_render_base_resume(fake_render: list[str]) -> None:
    user = make_user(with_resume=True)
    browser = Browser(user)
    base = ResumeRow.objects.for_user(user.pk).get()
    response = browser.post("/api/resume/render")
    assert response.status_code == 202, response.content
    body = response.json()
    assert body == {
        "ok": True,
        "runId": body["runId"],
        "message": "Rendering your resume to PDF. It takes a few seconds.",
    }
    run = reload(body["runId"], user)
    assert run.input == {"resumeId": str(base.id), "prompt": "Render your resume to PDF"}
    assert run.dedupe_key == f"render:{base.id}"

    run_deferred()
    run = reload(body["runId"], user)
    assert run.status == "completed", run.error
    assert run.output == {
        "resumeId": str(base.id),
        "documentId": out(run)["documentId"],
        "pages": 1,
        "density": "normal",
    }
    assert run.stats == {
        "phase": "done",
        "documentId": out(run)["documentId"],
        "pages": 1,
        "density": "normal",
    }
    [final] = events_of(run)
    assert (final.kind, final.message) == ("run.completed", "Rendered your resume to a 1-page PDF.")


@pytest.mark.django_db
def test_render_refusals() -> None:
    browser = Browser(make_user())
    response = browser.post("/api/resume/render")
    assert (response.status_code, response.json()) == (
        422,
        {"ok": False, "message": "There is no resume on file yet. Upload one first."},
    )
    gone = pipeline.start_render_run(browser.user.pk, uuid.uuid4())
    assert (gone.ok, gone.message) == (False, "That resume no longer exists.")
    # A tailored resume renders under its own prompt.
    tailored = make_resume(make_user(with_resume=True), kind="tailored")
    started = pipeline.start_render_run(tailored.user_id, tailored.id)
    run = Run.objects.for_user(tailored.user_id).get(id=started.run_id)
    assert run.input["prompt"] == "Render your tailored resume to PDF"


@pytest.mark.django_db(transaction=True)
def test_render_retries_once_and_keeps_the_run_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = make_user(with_resume=True)
    calls: list[str] = []
    states: list[str] = []

    async def flaky(user_id: str, resume_id: Any) -> RenderResult:
        calls.append(str(resume_id))
        states.append(await db(lambda: Run.objects.for_user(user_id).get().status))
        if len(calls) == 1:
            raise RuntimeError("Target closed")
        document = await db(_pdf_document, user_id, str(resume_id))
        return RenderResult(document_id=document.id, pages=2, density="compact", reused=False)

    monkeypatch.setattr(rendering, "render_resume", flaky)
    body = Browser(user).post("/api/resume/render").json()
    run_deferred()
    run = reload(body["runId"], user)
    assert run.status == "completed", run.error
    assert len(calls) == 2
    assert states == ["running", "running"]  # the retry found the run still running
    assert kinds(run) == ["run.completed"]
    assert events_of(run)[0].message == "Rendered your resume to a 2-page PDF."


@pytest.mark.django_db(transaction=True)
def test_render_fails_after_its_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    user = make_user(with_resume=True)
    calls: list[str] = []

    async def broken(user_id: str, resume_id: Any) -> RenderResult:
        calls.append(str(resume_id))
        raise RuntimeError("Target closed")

    monkeypatch.setattr(rendering, "render_resume", broken)
    body = Browser(user).post("/api/resume/render").json()
    run_deferred()
    run = reload(body["runId"], user)
    assert (run.status, run.error) == ("failed", "Could not render the PDF: Target closed")
    assert len(calls) == 2
    assert kinds(run) == ["run.failed"]


@pytest.mark.django_db(transaction=True)
def test_render_of_a_vanished_resume_does_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    user = make_user(with_resume=True)
    calls: list[str] = []

    async def gone(user_id: str, resume_id: Any) -> RenderResult:
        calls.append(str(resume_id))
        raise RenderError("That resume no longer exists.")

    monkeypatch.setattr(rendering, "render_resume", gone)
    body = Browser(user).post("/api/resume/render").json()
    run_deferred()
    run = reload(body["runId"], user)
    assert (run.status, run.error) == (
        "failed",
        "Could not render the PDF: That resume no longer exists.",
    )
    assert len(calls) == 1


def _chromium_available() -> str | None:
    async def probe() -> None:
        try:
            await rendering._CHROMIUM.browser()  # pyright: ignore[reportPrivateUsage]
        finally:
            await rendering.close_browser()

    try:
        asyncio.run(probe())
    except PlaywrightError as error:
        return str(error)
    return None


@pytest.fixture
def chromium() -> Iterator[None]:
    problem = _chromium_available()
    if problem is not None:
        pytest.skip(f"no Chromium to render with: {problem}")
    yield


def drain_and_close_browser() -> None:
    """`run_deferred()`, closing this loop's Chromium before the loop ends."""

    async def run() -> None:
        try:
            await arun_deferred()
        finally:
            await rendering.close_browser()

    asyncio.run(run())


@pytest.mark.slow
@pytest.mark.django_db(transaction=True)
def test_render_produces_a_served_pdf_and_reuses_it(chromium: None) -> None:
    """The Done check's render, in-process: a real PDF served with the right headers, and a
    second render that reuses it without rendering."""
    user = make_user(with_resume=True)
    browser = Browser(user)
    first = browser.post("/api/resume/render").json()
    drain_and_close_browser()
    run = reload(first["runId"], user)
    assert run.status == "completed", run.error
    document_id = out(run)["documentId"]
    assert out(run)["density"] in ("normal", "compact")

    served = browser.get(f"/api/documents/{document_id}")
    assert served.status_code == 200
    assert served.content.startswith(b"%PDF-")
    assert served["Content-Type"] == "application/pdf"
    assert served["Content-Length"] == str(len(served.content))
    resume = Resume.model_validate(ResumeRow.objects.for_user(user.pk).get().data)
    name = f"{resume.basics.name} - Resume.pdf"
    assert served["Content-Disposition"].startswith(f'inline; filename="{name}"')
    assert served["Cache-Control"] == "private, no-store"
    assert served["X-Content-Type-Options"] == "nosniff"
    pages = len(pdfium.PdfDocument(served.content))
    assert out(run)["pages"] == pages
    download = browser.get(f"/api/documents/{document_id}?download=1")
    assert download["Content-Disposition"].startswith(f'attachment; filename="{name}"')

    second = browser.post("/api/resume/render").json()
    assert second["runId"] != first["runId"]
    drain_and_close_browser()
    again = reload(second["runId"], user)
    assert again.status == "completed", again.error
    assert out(again)["documentId"] == document_id  # reused: no second render
    assert out(again)["density"] is None
    assert out(again)["pages"] == pages
    assert Document.objects.for_user(user.pk).filter(kind="resume_pdf").count() == 1


# --- preview and the editor ----------------------------------------------------------------------


@pytest.mark.django_db
def test_preview_headers_and_refusals() -> None:
    user = make_user(with_resume=True)
    browser = Browser(user)
    base = ResumeRow.objects.for_user(user.pk).get()
    response = browser.get(f"/api/resumes/{base.id}/preview")
    assert response.status_code == 200
    assert response["Content-Type"] == "text/html; charset=utf-8"
    assert response["Content-Security-Policy"] == (
        "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'self'"
    )
    assert response["Cache-Control"] == "private, no-store"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["Referrer-Policy"] == "no-referrer"
    assert b"<!doctype html>" in response.content.lower()

    other = Browser(make_user())
    assert other.get(f"/api/resumes/{base.id}/preview").status_code == 404
    assert browser.get("/api/resumes/not-a-uuid/preview").status_code == 404
    broken = make_resume(user, data={"work": "not a list"})
    unreadable = browser.get(f"/api/resumes/{broken.id}/preview")
    assert (unreadable.status_code, unreadable.json()) == (
        422,
        {"ok": False, "message": "This resume could not be read."},
    )


@pytest.mark.django_db
def test_put_resume_saves_versions() -> None:
    user = make_user(with_resume=True)
    browser = Browser(user)
    stored = ResumeRow.objects.for_user(user.pk).get().data

    unchanged = browser.put("/api/resume", stored)
    assert (unchanged.status_code, unchanged.json()) == (
        200,
        {"ok": True, "message": "Profile saved"},
    )
    assert ResumeRow.objects.for_user(user.pk).count() == 1  # nothing changed: nothing written

    edited = json.loads(json.dumps(stored))
    edited["work"][0]["highlights"][0] = "Rewrote the billing service in Go."
    assert browser.put("/api/resume", edited).status_code == 200
    newest = ResumeRow.objects.for_user(user.pk).order_by("-version").first()
    assert newest is not None and newest.version == 2
    data = Resume.model_validate(newest.data)
    assert data.work[0].source == "user"
    assert {item.source for item in data.work[1:]} == {"resume"}


@pytest.mark.django_db
def test_put_resume_refusals() -> None:
    user = make_user(with_resume=True)
    browser = Browser(user)
    stored = ResumeRow.objects.for_user(user.pk).get().data

    too_long = json.loads(json.dumps(stored))
    too_long["work"][1]["startDate"] = "x" * 41
    response = browser.put("/api/resume", too_long)
    assert (response.status_code, response.json()) == (
        400,
        {
            "ok": False,
            "message": "Experience item 2: the start date is too long (40 characters at most).",
        },
    )

    cases = {
        "Experience item 1: the company is missing.": ("work", 0, "company", None),
        "Education item 1: the degree should be text.": ("education", 0, "studyType", 5),
        "Skill 2: the name should be text.": ("skills", 1, "name", ["x"]),
        "Project 1: the end date is empty.": ("projects", 0, "endDate", ""),
    }
    for message, (section, index, field, value) in cases.items():
        body = json.loads(json.dumps(stored))
        if not body[section]:
            continue
        if value is None:
            del body[section][index][field]
        else:
            body[section][index][field] = value
        answer = browser.put("/api/resume", body)
        assert (answer.status_code, answer.json()["message"]) == (400, message)

    link = json.loads(json.dumps(stored))
    link["basics"]["links"] = [{"id": "l1", "label": "Site"}]
    assert browser.put("/api/resume", link).json()["message"] == "Link 1: the link is missing."
    assert browser.put("/api/resume", [1, 2]).json()["message"] == (
        "That resume could not be saved. Check the fields and try again."
    )
    assert ResumeRow.objects.for_user(user.pk).count() == 1

    newcomer = Browser(make_user())
    refused = newcomer.put("/api/resume", stored)
    assert (refused.status_code, refused.json()["message"]) == (
        422,
        "Upload a resume before editing your profile.",
    )


@pytest.mark.slow
@pytest.mark.django_db(transaction=True)
def test_tailor_with_a_real_render(
    chromium: None, fakes: None, tailor_setup: Callable[..., tuple[Browser, JobMatch]]
) -> None:
    """Tailoring renders inline, as today: the run completes with a served PDF named for the
    company, and the tailored version points at it."""
    browser, match = tailor_setup()
    body = browser.post("/api/resumes/tailor", {"matchId": str(match.id)}).json()
    drain_and_close_browser()
    run = reload(body["runId"], browser.user)
    assert run.status == "completed", run.error
    assert out(run)["documentId"] is not None and out(run)["pages"] >= 1
    served = browser.get(f"/api/documents/{out(run)['documentId']}")
    assert served.status_code == 200 and served.content.startswith(b"%PDF-")
    assert f" - Resume - {match.job.company_name}.pdf" in served["Content-Disposition"]
    tailored = ResumeRow.objects.for_user(browser.user.pk).get(kind=ResumeKind.TAILORED)
    assert str(tailored.rendered_document_id) == out(run)["documentId"]
    assert kinds(run) == ["tailor.started", "tailor.verified", "run.completed"]
