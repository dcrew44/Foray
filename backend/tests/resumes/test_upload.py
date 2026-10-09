"""`POST /api/uploads` and `GET /api/documents/{id}` (design §11.1, §11.7, §5.2): magic bytes, the
zip-bomb guard, the size cap, dedupe reuse, no orphan on failure, the throttle, and the file
response's headers."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.test.client import AsyncClient

from apps.accounts.models import User
from apps.core import throttle
from apps.core.storage import storage
from apps.resumes import pipeline
from apps.resumes.api import content_disposition
from apps.resumes.models import Document
from apps.resumes.parse.sniff import (
    DOCX_MIME,
    MAX_DOCX_ENTRIES,
    MAX_UPLOAD_BYTES,
    UNREADABLE_WORD,
    UNSUPPORTED,
    UploadRejected,
    sniff,
    too_large_message,
)
from apps.runs.catalogue import RunKind
from apps.runs.models import Run
from tests.factories import make_document, make_user
from tests.queue import deferred

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "resumes"
ORIGIN = "http://localhost:3000"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def docx_bytes(entries: dict[str, bytes] | None = None, *, extra: int = 0) -> bytes:
    """A ZIP with the two parts that make it a Word document, plus `entries` and `extra`
    filler entries."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<w:document/>")
        for name, body in (entries or {}).items():
            archive.writestr(name, body)
        for index in range(extra):
            archive.writestr(f"word/media/filler-{index}.bin", b"")
    return buffer.getvalue()


class Uploader:
    """A signed-in browser: multipart uploads and plain GETs, with the Origin header the
    origin check wants on unsafe methods (§4.3)."""

    def __init__(self, client: Client, user: User) -> None:
        self.client = client
        self.user = user

    def upload(
        self, data: bytes, name: str = "resume.pdf", content_type: str = "application/pdf"
    ) -> Any:
        return self.client.post(
            "/api/uploads",
            {"file": SimpleUploadedFile(name, data, content_type=content_type)},
            headers={"Origin": ORIGIN},
        )

    def get(self, path: str) -> Any:
        return self.client.get(path)


@pytest.fixture
def signed_in() -> Callable[..., Uploader]:
    def make(**fields: Any) -> Uploader:
        user = make_user(**fields)
        client = Client()
        client.force_login(user)
        return Uploader(client, user)

    return make


@pytest.fixture(autouse=True)
def _storage_dir(settings: Any, tmp_path: Path) -> Path:
    settings.STORAGE_DIR = str(tmp_path / "storage")
    return tmp_path / "storage"


def stored_files(root: Path, user: User) -> list[Path]:
    folder = root / user.pk
    return sorted(path for path in folder.rglob("*") if path.is_file()) if folder.exists() else []


# --- sniffing ------------------------------------------------------------------------------------


def test_sniff_reads_magic_bytes() -> None:
    assert sniff(fixture("two-column.pdf")) == "pdf"
    assert sniff(fixture("plain.docx")) == "docx"
    assert sniff(docx_bytes()) == "docx"
    refused = [
        b"MZ\x90\x00\x03\x00\x00\x00",  # a Windows executable
        b"",
        b"Dana Reyes\nSoftware engineer",
        b" %PDF-1.7",  # the magic is at the start or nowhere
        b"PK\x03\x04 not really a zip",
        zipfile_with({"word/document.xml": b"<w:document/>"}),  # no content types
        zipfile_with({"[Content_Types].xml": b"<Types/>", "xl/workbook.xml": b"<x/>"}),  # Excel
    ]
    for data in refused:
        with pytest.raises(UploadRejected) as caught:
            sniff(data)
        assert (caught.value.status, caught.value.message) == (415, UNSUPPORTED), data[:20]


def zipfile_with(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, body in entries.items():
            archive.writestr(name, body)
    return buffer.getvalue()


def test_docx_guard_refuses_zip_bombs() -> None:
    # 51 MB of zeros inflates from about 50 kB: the declared size gives it away.
    bomb = docx_bytes({"word/media/zeros.bin": bytes(51 * 1024 * 1024)})
    assert len(bomb) < MAX_UPLOAD_BYTES
    many = docx_bytes(extra=MAX_DOCX_ENTRIES - 1)  # 2 parts + 1,999 = 2,001 entries
    for data in (bomb, many):
        with pytest.raises(UploadRejected) as caught:
            sniff(data)
        assert (caught.value.status, caught.value.message) == (415, UNREADABLE_WORD)
    # At the limits it is a Word file.
    assert sniff(docx_bytes(extra=MAX_DOCX_ENTRIES - 2)) == "docx"
    assert sniff(docx_bytes({"word/media/zeros.bin": bytes(49 * 1024 * 1024)})) == "docx"


def test_too_large_message_rounds_like_to_fixed() -> None:
    mb = 1024 * 1024
    assert too_large_message(MAX_UPLOAD_BYTES + 1) == "That file is 10.0 MB; the limit is 10 MB."
    assert too_large_message(int(12.3 * mb)) == "That file is 12.3 MB; the limit is 10 MB."
    # An exact half rounds up, as `toFixed` does (Python's own format would print 10.2).
    assert too_large_message(int(10.25 * mb)) == "That file is 10.3 MB; the limit is 10 MB."


# --- POST /api/uploads ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_upload_answers_202_and_queues_an_import(
    signed_in: Callable[..., Uploader], _storage_dir: Path
) -> None:
    browser = signed_in()
    data = fixture("two-column.pdf")
    response = browser.upload(data, "Dana Reyes résumé.pdf")
    assert response.status_code == 202, response.content
    body = response.json()
    assert body == {
        "ok": True,
        "runId": body["runId"],
        "documentId": body["documentId"],
        "message": "Reading your resume…",
    }

    document = Document.objects.for_user(browser.user.pk).get(id=body["documentId"])
    assert document.kind == "resume_upload"
    assert document.filename == "Dana Reyes résumé.pdf"
    assert document.mime == "application/pdf"
    assert document.size_bytes == len(data)
    assert document.sha256 == hashlib.sha256(data).hexdigest()
    assert document.text_extracted is None  # the run reads it
    assert document.storage_path.startswith(f"{browser.user.pk}/resume_upload/")
    with storage().open(document.storage_path) as handle:
        assert handle.read() == data

    run = Run.objects.for_user(browser.user.pk).get(id=body["runId"])
    assert (run.kind, run.status, run.dedupe_key) == (
        RunKind.PROFILE_IMPORT,
        "queued",
        f"import:{document.id}",
    )
    assert run.input == {
        "documentId": str(document.id),
        "filename": "Dana Reyes résumé.pdf",
        "prompt": "Read your resume",
    }
    [job] = deferred("resume.import")
    assert job.queue == "interactive"
    assert job.args == {"run_id": str(run.id)}
    assert job.lock == job.queueing_lock == f"run:{run.id}"


@pytest.mark.django_db
def test_upload_sniffs_bytes_not_names(
    signed_in: Callable[..., Uploader], _storage_dir: Path
) -> None:
    browser = signed_in()
    # A real PDF with a misleading name and type is still a PDF.
    renamed = browser.upload(fixture("single-column.pdf"), "resume.exe", "application/x-msdownload")
    assert renamed.status_code == 202
    assert Document.objects.for_user(browser.user.pk).get().mime == "application/pdf"

    # A Word file is stored with the Word type, whatever the browser said.
    word = browser.upload(fixture("plain.docx"), "cv.docx", "application/octet-stream")
    assert word.status_code == 202
    assert Document.objects.for_user(browser.user.pk).get(id=word.json()["documentId"]).mime == (
        DOCX_MIME
    )

    # A renamed executable is refused, and nothing is stored or started.
    before = stored_files(_storage_dir, browser.user)
    exe = browser.upload(b"MZ\x90\x00" + bytes(2048), "resume.pdf", "application/pdf")
    assert exe.status_code == 415
    assert exe.json() == {"ok": False, "message": "Upload a PDF or a Word (.docx) file."}
    assert stored_files(_storage_dir, browser.user) == before
    assert Document.objects.for_user(browser.user.pk).count() == 2
    assert Run.objects.for_user(browser.user.pk).count() == 2


@pytest.mark.django_db
def test_upload_refuses_a_zip_bomb(signed_in: Callable[..., Uploader], _storage_dir: Path) -> None:
    browser = signed_in()
    bomb = docx_bytes({"word/media/zeros.bin": bytes(60 * 1024 * 1024)})
    response = browser.upload(bomb, "resume.docx", DOCX_MIME)
    assert response.status_code == 415
    assert response.json() == {"ok": False, "message": "That Word file could not be read."}
    assert stored_files(_storage_dir, browser.user) == []
    assert not Document.objects.for_user(browser.user.pk).exists()


@pytest.mark.django_db
def test_upload_size_cap(signed_in: Callable[..., Uploader], _storage_dir: Path) -> None:
    browser = signed_in()
    over = browser.upload(b"%PDF-1.7\n" + bytes(MAX_UPLOAD_BYTES))
    assert over.status_code == 413
    assert over.json() == {"ok": False, "message": "That file is 10.0 MB; the limit is 10 MB."}

    big = browser.upload(b"%PDF-1.7\n" + bytes(int(12.3 * 1024 * 1024) - 9))
    assert big.status_code == 413
    assert big.json()["message"] == "That file is 12.3 MB; the limit is 10 MB."
    assert stored_files(_storage_dir, browser.user) == []

    # Exactly 10 MB is allowed (the TS check was `size > MAX_BYTES`).
    exact = browser.upload(b"%PDF-1.7\n" + bytes(MAX_UPLOAD_BYTES - 9))
    assert exact.status_code == 202
    assert Document.objects.for_user(browser.user.pk).get().size_bytes == MAX_UPLOAD_BYTES


@pytest.mark.django_db
def test_upload_without_a_file(signed_in: Callable[..., Uploader]) -> None:
    browser = signed_in()
    other_field = browser.client.post(
        "/api/uploads", {"kind": "linkedin_pdf"}, headers={"Origin": ORIGIN}
    )
    assert other_field.status_code == 400
    assert other_field.json() == {"ok": False, "message": "No file uploaded."}
    as_json = browser.client.post(
        "/api/uploads",
        data=json.dumps({"file": "resume.pdf"}),
        content_type="application/json",
        headers={"Origin": ORIGIN},
    )
    assert (as_json.status_code, as_json.json()["message"]) == (400, "No file uploaded.")


@pytest.mark.django_db
def test_upload_dedupe_reuses_the_document(
    signed_in: Callable[..., Uploader], _storage_dir: Path
) -> None:
    browser = signed_in()
    data = fixture("two-column.pdf")
    first = browser.upload(data, "resume.pdf").json()
    again = browser.upload(data, "resume (1).pdf").json()
    # The same bytes are the same document, and its import is still queued: the same run.
    assert again["documentId"] == first["documentId"]
    assert again["runId"] == first["runId"]
    assert len(stored_files(_storage_dir, browser.user)) == 1  # no second copy
    assert len(deferred("resume.import")) == 1

    # Once that import has finished, a re-upload reads the same document again.
    Run.objects.for_user(browser.user.pk).filter(id=first["runId"]).update(status="completed")
    later = browser.upload(data, "resume.pdf").json()
    assert later["documentId"] == first["documentId"]
    assert later["runId"] != first["runId"]
    assert len(stored_files(_storage_dir, browser.user)) == 1
    assert Document.objects.for_user(browser.user.pk).count() == 1


@pytest.mark.django_db
def test_reupload_restores_a_missing_stored_file(
    signed_in: Callable[..., Uploader], _storage_dir: Path
) -> None:
    """A document whose stored copy went missing is repaired by uploading the same bytes again,
    instead of every re-upload reusing a document that can never be read."""
    browser = signed_in()
    data = fixture("two-column.pdf")
    first = browser.upload(data).json()
    document = Document.objects.for_user(browser.user.pk).get()
    storage().delete(document.storage_path)
    Run.objects.for_user(browser.user.pk).update(status="failed")

    again = browser.upload(data).json()
    assert again["documentId"] == first["documentId"]
    repaired = Document.objects.for_user(browser.user.pk).get()
    assert repaired.storage_path != document.storage_path
    with storage().open(repaired.storage_path) as handle:
        assert handle.read() == data
    assert len(stored_files(_storage_dir, browser.user)) == 1


@pytest.mark.django_db
def test_upload_leaves_no_orphan_when_its_transaction_fails(
    signed_in: Callable[..., Uploader], _storage_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = signed_in()

    def broken_start(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the queue is down")

    monkeypatch.setattr(pipeline.lifecycle, "start_run", broken_start)
    response = browser.upload(fixture("two-column.pdf"))
    assert response.status_code == 500
    assert response.json()["ok"] is False
    assert "queue" not in response.json()["message"]  # nothing internal leaks
    assert stored_files(_storage_dir, browser.user) == []
    assert not Document.objects.for_user(browser.user.pk).exists()


@pytest.mark.django_db
def test_concurrent_identical_upload_reuses_the_winner(
    signed_in: Callable[..., Uploader], _storage_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two uploads of the same bytes race: the loser's insert hits `documents_user_sha256`, its
    file is deleted, and it reuses the winner's document."""
    browser = signed_in()
    data = fixture("single-column.pdf")
    real_put = type(storage()).put
    winner: list[Document] = []

    winner_key = f"{browser.user.pk}/resume_upload/1-winner.pdf"

    def racing_put(self: Any, key: str, body: bytes) -> str:
        if not winner:  # the other request stores its file and commits its document right now
            real_put(self, winner_key, body)
            winner.append(
                make_document(
                    browser.user, content=body, filename="resume.pdf", storage_path=winner_key
                )
            )
        return real_put(self, key, body)

    monkeypatch.setattr(type(storage()), "put", racing_put)
    response = browser.upload(data)
    assert response.status_code == 202
    assert response.json()["documentId"] == str(winner[0].id)
    # The loser's copy was deleted; only the winner's file is left.
    assert stored_files(_storage_dir, browser.user) == [_storage_dir / winner_key]
    assert Document.objects.for_user(browser.user.pk).count() == 1


@pytest.mark.django_db
def test_upload_throttle(signed_in: Callable[..., Uploader], _storage_dir: Path) -> None:
    browser = signed_in()
    limit = throttle.UPLOAD
    for _ in range(limit.limit):
        throttle.throttle(f"upload:{browser.user.pk}", limit.limit, limit.window_s)
    response = browser.upload(fixture("two-column.pdf"))
    assert response.status_code == 429
    assert response.json() == {
        "ok": False,
        "message": "Too many attempts. Try again in a few minutes.",
    }
    assert stored_files(_storage_dir, browser.user) == []
    # Another user is not affected.
    assert signed_in().upload(fixture("two-column.pdf")).status_code == 202


@pytest.mark.django_db
def test_upload_needs_a_session_and_a_same_origin_request() -> None:
    client = Client()
    data = {"file": SimpleUploadedFile("resume.pdf", b"%PDF-1.7", content_type="application/pdf")}
    response = client.post("/api/uploads", data, headers={"Origin": ORIGIN})
    assert (response.status_code, response.json()["message"]) == (401, "Not signed in.")
    client.force_login(make_user())
    cross = client.post("/api/uploads", data, headers={"Origin": "http://evil.example"})
    assert cross.status_code == 403


@pytest.mark.django_db(transaction=True)
async def test_upload_through_asgi(async_client: AsyncClient, settings: Any, tmp_path: Path):
    """Under uvicorn the request is an `ASGIRequest`: the size handler and the parse behave the
    same."""
    settings.STORAGE_DIR = str(tmp_path / "asgi-storage")
    user = await User.objects.acreate(email="asgi-upload@example.test", name="Asgi")
    await async_client.aforce_login(user)  # pyright: ignore[reportAttributeAccessIssue]

    def form(data: bytes) -> dict[str, Any]:
        return {"file": SimpleUploadedFile("resume.pdf", data, content_type="application/pdf")}

    ok = await async_client.post(
        "/api/uploads", form(fixture("two-column.pdf")), headers={"Origin": ORIGIN}
    )
    assert ok.status_code == 202, ok.content
    too_big = await async_client.post(
        "/api/uploads", form(b"%PDF-1.7\n" + bytes(MAX_UPLOAD_BYTES)), headers={"Origin": ORIGIN}
    )
    assert too_big.status_code == 413
    assert too_big.json()["message"] == "That file is 10.0 MB; the limit is 10 MB."


# --- GET /api/documents/{id} ---------------------------------------------------------------------


def test_content_disposition_matches_the_typescript() -> None:
    # Expected values printed by Node 26 with today's `contentDisposition`.
    name = "Zoë Müller 🎉 - Resume (1)*'.pdf"
    assert content_disposition("inline", name) == (
        'inline; filename="Zo_ M_ller __ - Resume (1)*\'.pdf"; '
        "filename*=UTF-8''Zo%C3%AB%20M%C3%BCller%20%F0%9F%8E%89%20-%20Resume%20%281%29%2A%27.pdf"
    )
    assert content_disposition("attachment", 'a"b\\c~!.pdf') == (
        "attachment; filename=\"a_b_c~!.pdf\"; filename*=UTF-8''a%22b%5Cc~!.pdf"
    )


@pytest.mark.django_db
def test_document_download_headers(signed_in: Callable[..., Uploader]) -> None:
    browser = signed_in()
    data = fixture("single-column.pdf")
    key = f"{browser.user.pk}/resume_pdf/1-Zoe.pdf"
    storage().put(key, data)
    document = make_document(
        browser.user,
        kind="resume_pdf",
        filename="Zoë Müller - Resume.pdf",
        content=data,
        storage_path=key,
    )

    inline = browser.get(f"/api/documents/{document.id}")
    assert inline.status_code == 200
    assert inline.content == data
    assert inline["Content-Type"] == "application/pdf"
    assert inline["Content-Length"] == str(len(data))
    assert inline["Content-Disposition"] == (
        'inline; filename="Zo_ M_ller - Resume.pdf"; '
        "filename*=UTF-8''Zo%C3%AB%20M%C3%BCller%20-%20Resume.pdf"
    )
    assert inline["Cache-Control"] == "private, no-store"
    assert inline["X-Content-Type-Options"] == "nosniff"

    download = browser.get(f"/api/documents/{document.id}?download=1")
    assert download["Content-Disposition"].startswith('attachment; filename="Zo_ M_ller')
    assert browser.get(f"/api/documents/{document.id}?download=0")[
        "Content-Disposition"
    ].startswith("inline;")


@pytest.mark.django_db
def test_document_refusals(signed_in: Callable[..., Uploader]) -> None:
    owner, other = signed_in(), signed_in()
    key = f"{owner.user.pk}/resume_upload/1-resume.pdf"
    storage().put(key, b"%PDF-1.7 owner")
    document = make_document(owner.user, content=b"%PDF-1.7 owner", storage_path=key)
    missing = make_document(owner.user, storage_path=f"{owner.user.pk}/resume_upload/2-gone.pdf")

    not_found = {"ok": False, "message": "That no longer exists."}
    assert other.get(f"/api/documents/{document.id}").json() == not_found
    assert other.get(f"/api/documents/{document.id}").status_code == 404
    assert owner.get("/api/documents/not-a-uuid").status_code == 404
    gone = owner.get(f"/api/documents/{missing.id}")
    assert (gone.status_code, gone.json()) == (
        404,
        {"ok": False, "message": "The file is missing from storage."},
    )
    assert owner.get(f"/api/documents/{document.id}").status_code == 200
