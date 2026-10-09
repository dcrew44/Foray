"""PDF rendering (design §11.6). The browser tests are marked `slow`: they need Chromium (the
bundled one, `PDF_BROWSER_CHANNEL`, or a local Chrome) and are skipped when none launches."""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import time
from collections.abc import AsyncIterator
from typing import Any, cast

import pypdfium2 as pdfium
import pytest
from playwright.async_api import Error as PlaywrightError

from apps.core.storage import storage
from apps.resumes import render
from apps.resumes.models import Document
from apps.resumes.models import Resume as ResumeRow
from apps.resumes.parse.pdf import parse_pdf
from apps.resumes.render import (
    RENDER_VERSION,
    FittedPdf,
    RenderedPdf,
    pdf_filename,
    render_fitted,
    render_hash,
    render_pdf,
    render_resume,
)
from apps.resumes.schema import Resume
from apps.resumes.template import render_resume_html
from tests.factories import make_job, make_match, make_resume, make_user, sample_resume_data
from tests.resumes.support import FIXTURES, golden


@pytest.fixture
async def chromium() -> AsyncIterator[None]:
    try:
        await render._CHROMIUM.browser()
    except PlaywrightError as error:
        pytest.skip(f"no Chromium to render with: {error}")
    yield
    await render.close_browser()


def sample() -> Resume:
    return Resume.model_validate(sample_resume_data())


# --- Without a browser ---------------------------------------------------------------------------


def test_render_hash_is_the_normal_html_and_the_version():
    resume = sample()
    expected = hashlib.sha256(f"{render_resume_html(resume)}|{RENDER_VERSION}".encode()).hexdigest()
    assert render_hash(resume) == expected
    edited = resume.model_copy(deep=True)
    edited.work[0].highlights.append("One more line.")
    assert render_hash(edited) != expected
    # A tailored resume's key also names its download: its document carries that name.
    named = f"{render_resume_html(resume)}|{RENDER_VERSION}|{pdf_filename(resume, 'Stripe')}"
    assert render_hash(resume, "Stripe") == hashlib.sha256(named.encode()).hexdigest()
    assert render_hash(resume, "Stripe") != expected


async def test_fitting_keeps_compact_only_when_it_saves_a_page(monkeypatch: pytest.MonkeyPatch):
    pages = {"normal": 2, "compact": 1}

    async def fake_render(html: str) -> RenderedPdf:
        density = "compact" if '<main class="page compact">' in html else "normal"
        return RenderedPdf(pdf=density.encode(), pages=pages[density])

    monkeypatch.setattr(render, "render_pdf", fake_render)
    fitted = await render_fitted(sample())
    assert (fitted.density, fitted.pages, fitted.pdf) == ("compact", 1, b"compact")

    pages["compact"] = 2
    assert (await render_fitted(sample())).density == "normal"

    pages["normal"] = 1
    calls: list[str] = []

    async def counting(html: str) -> RenderedPdf:
        calls.append(html)
        return await fake_render(html)

    monkeypatch.setattr(render, "render_pdf", counting)
    assert (await render_fitted(sample())).density == "normal"
    assert len(calls) == 1  # one page already: no compact render


class _HungBrowser:
    async def close(self) -> None:
        await asyncio.Event().wait()  # never answers


class _Driver:
    def __init__(self, *, hangs: bool = False) -> None:
        self.hangs = hangs
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True
        if self.hangs:
            await asyncio.Event().wait()


async def test_closing_gives_up_on_a_browser_that_never_answers(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """A close that never returns (Chromium already gone while the driver waits on it) is
    bounded, and the driver is still stopped afterwards; a stuck stop is bounded too."""
    monkeypatch.setattr(render, "CLOSE_TIMEOUT_S", 0.2)
    for driver in (_Driver(), _Driver(hangs=True)):
        chromium = render._Chromium()
        chromium._for_this_loop()  # binds it to this loop, as a launch would
        chromium._browser = cast(Any, _HungBrowser())
        chromium._playwright = cast(Any, driver)
        started = time.monotonic()
        with caplog.at_level(logging.WARNING, logger=render.__name__):
            # The outer bound turns a regression into a failure instead of a hung suite.
            await asyncio.wait_for(chromium.close(), 5)
        assert time.monotonic() - started < 2
        assert driver.stopped
        assert (chromium._browser, chromium._playwright) == (None, None)
    warnings = [record.getMessage() for record in caplog.records]
    stuck_close = "render: could not close the browser within 0.2s; going on without it"
    stuck_stop = "render: could not stop the Playwright driver within 0.2s; going on without it"
    assert (warnings.count(stuck_close), warnings.count(stuck_stop)) == (2, 1)


def test_pdf_filename():
    resume = sample()
    resume.basics.name = 'Dana "DR" Reyes/\x07'
    assert pdf_filename(resume) == "Dana DR Reyes - Resume.pdf"
    assert pdf_filename(resume, "Acme:  Labs") == "Dana DR Reyes - Resume - Acme Labs.pdf"
    resume.basics.name = "  "
    assert pdf_filename(resume) == "Resume.pdf"


# --- With a browser ------------------------------------------------------------------------------


@pytest.mark.slow
async def test_single_column_fixture_renders_to_the_ts_page_count(chromium: None):
    html = (FIXTURES / "resumes/single-column.html").read_text()
    rendered = await render_pdf(html)
    assert rendered.pages == golden("resumes/pdf/single-column.json")["pageCount"] == 2
    assert rendered.pdf.startswith(b"%PDF-")
    assert parse_pdf(rendered.pdf).text.startswith("Dana Whitlock Reyes")


@pytest.mark.slow
@pytest.mark.django_db(transaction=True)
async def test_a_second_render_reuses_the_document_by_render_hash(
    chromium: None, monkeypatch: pytest.MonkeyPatch
):
    user = await _sync(make_user, with_resume=True)
    row = await _sync(lambda: ResumeRow.objects.for_user(user.pk).get(kind="base"))

    first = await render_resume(user.pk, row.id)
    assert first.reused is False
    assert first.density in ("normal", "compact")
    document = await _sync(lambda: Document.objects.for_user(user.pk).get(id=first.document_id))
    assert document.kind == "resume_pdf"
    assert document.mime == "application/pdf"
    assert document.render_hash == render_hash(Resume.model_validate(row.data))
    assert document.text_extracted is None  # rendering never touches extracted text
    assert document.filename == "Avery Quinn - Resume.pdf"
    with storage().open(document.storage_path) as handle:
        stored = handle.read()
    assert hashlib.sha256(stored).hexdigest() == document.sha256
    assert len(stored) == document.size_bytes
    refreshed = await _sync(lambda: ResumeRow.objects.for_user(user.pk).get(id=row.id))
    assert refreshed.rendered_document_id == document.id

    async def no_render(*_: Any) -> Any:
        raise AssertionError("a reused render must not launch the browser")

    monkeypatch.setattr(render, "_render_once", no_render)
    monkeypatch.setattr(render._CHROMIUM, "browser", no_render)
    second = await render_resume(user.pk, row.id)
    assert second.reused is True
    assert second.document_id == first.document_id
    assert second.pages == first.pages
    assert second.density is None
    count = await _sync(lambda: Document.objects.for_user(user.pk).count())
    assert count == 1


@pytest.mark.slow
@pytest.mark.django_db(transaction=True)
async def test_a_tailored_pdf_is_named_for_the_company_and_a_lost_file_is_rendered_again(
    chromium: None,
):
    user = await _sync(make_user)
    job = await _sync(make_job, company_name="Stripe")
    match = await _sync(make_match, user, job=job)
    row = await _sync(make_resume, user, kind="tailored", match=match)

    first = await render_resume(user.pk, row.id)
    document = await _sync(lambda: Document.objects.for_user(user.pk).get(id=first.document_id))
    assert document.filename.endswith(" - Resume - Stripe.pdf")

    storage().delete(document.storage_path)
    again = await render_resume(user.pk, row.id)
    assert again.reused is False
    assert again.document_id == first.document_id
    repaired = await _sync(lambda: Document.objects.for_user(user.pk).get(id=first.document_id))
    assert storage().exists(repaired.storage_path)


def _blank_pdf(width: int) -> bytes:
    """A real one-page PDF (the reuse path counts its pages)."""
    document = pdfium.PdfDocument.new()
    try:
        document.new_page(width, 792).close()
        buffer = io.BytesIO()
        document.save(buffer)
        return buffer.getvalue()
    finally:
        document.close()


async def _base_and_identical_tailored() -> tuple[Any, ResumeRow, ResumeRow]:
    """A user whose tailored resume (for Stripe) has exactly the base's data, as a tailoring
    that changed nothing leaves it: both render to the same HTML."""
    user = await _sync(make_user, with_resume=True)
    base = await _sync(lambda: ResumeRow.objects.for_user(user.pk).get(kind="base"))
    match = await _sync(make_match, user, job=await _sync(make_job, company_name="Stripe"))
    tailored = await _sync(make_resume, user, kind="tailored", match=match, data=base.data)
    return user, base, tailored


@pytest.mark.django_db(transaction=True)
async def test_a_tailored_pdf_identical_to_the_base_still_downloads_under_its_own_name(
    monkeypatch: pytest.MonkeyPatch,
):
    user, base, tailored = await _base_and_identical_tailored()
    renders: list[int] = []

    async def fitted(resume: Resume) -> FittedPdf:
        renders.append(len(renders))
        return FittedPdf(pdf=_blank_pdf(612 + len(renders)), pages=1, density="normal")

    monkeypatch.setattr(render, "render_fitted", fitted)
    first = await render_resume(user.pk, base.id)
    second = await render_resume(user.pk, tailored.id)
    assert second.document_id != first.document_id and not second.reused
    names = await _sync(
        lambda: dict(Document.objects.for_user(user.pk).values_list("id", "filename"))
    )
    assert names == {
        first.document_id: "Avery Quinn - Resume.pdf",
        second.document_id: "Avery Quinn - Resume - Stripe.pdf",
    }
    pointers = await _sync(
        lambda: dict(ResumeRow.objects.for_user(user.pk).values_list("id", "rendered_document"))
    )
    assert pointers == {base.id: first.document_id, tailored.id: second.document_id}
    # Each is reused by its own key afterwards: no further render.
    again = await render_resume(user.pk, tailored.id)
    assert (again.document_id, again.reused) == (second.document_id, True)
    assert (await render_resume(user.pk, base.id)).document_id == first.document_id
    assert renders == [0, 1]


@pytest.mark.django_db(transaction=True)
async def test_a_tailored_pdf_byte_identical_to_the_base_reuses_that_document(
    monkeypatch: pytest.MonkeyPatch,
):
    """Rendered within the same second, the two PDFs are byte-identical, and there is one row
    per bytes (`documents_user_sha256`): the tailored resume then uses the base's document
    rather than failing, and its stored copy is not left behind."""
    user, base, tailored = await _base_and_identical_tailored()
    same = _blank_pdf(612)  # made once: PDFium gives every document a fresh /ID

    async def fitted(resume: Resume) -> FittedPdf:
        return FittedPdf(pdf=same, pages=1, density="normal")

    monkeypatch.setattr(render, "render_fitted", fitted)
    first = await render_resume(user.pk, base.id)
    second = await render_resume(user.pk, tailored.id)
    assert (second.document_id, second.reused) == (first.document_id, True)
    paths = await _sync(
        lambda: list(Document.objects.for_user(user.pk).values_list("storage_path", flat=True))
    )
    assert len(paths) == 1
    stored = [p for p in (storage().root / user.pk / "resume_pdf").iterdir()]
    assert [str(p.relative_to(storage().root)) for p in stored] == paths
    row = await _sync(lambda: ResumeRow.objects.for_user(user.pk).get(id=tailored.id))
    assert row.rendered_document_id == first.document_id


@pytest.mark.django_db(transaction=True)
async def test_a_missing_or_unreadable_resume_is_a_plain_error():
    user = await _sync(make_user)
    with pytest.raises(render.RenderError, match=r"^That resume no longer exists\.$"):
        await render_resume(user.pk, "00000000-0000-7000-8000-000000000000")
    broken = await _sync(make_resume, user, data={"work": "not a list"})
    with pytest.raises(render.RenderError, match=r"^The stored resume could not be read\.$"):
        await render_resume(user.pk, broken.id)


async def _sync(fn: Any, *args: Any, **kwargs: Any) -> Any:
    from apps.core.db import db

    return await db(fn, *args, **kwargs)
