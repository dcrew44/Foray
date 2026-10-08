"""PDF rendering (design §11.6). The browser tests are marked `slow`: they need Chromium (the
bundled one, `PDF_BROWSER_CHANNEL`, or a local Chrome) and are skipped when none launches."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from playwright.async_api import Error as PlaywrightError

from apps.core.storage import storage
from apps.resumes import render
from apps.resumes.models import Document
from apps.resumes.models import Resume as ResumeRow
from apps.resumes.parse.pdf import parse_pdf
from apps.resumes.render import (
    RENDER_VERSION,
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
