"""HTML in, PDF out, with headless Chromium (design §11.6; replaces `src/worker/pdf.ts` and
`renderStoredResume`). Worker only: the API never loads a browser.

**Browser.** Playwright's async API drives the same playwright-core driver as the TS worker, so
PDFs match byte for byte except for their dates. One Chromium per worker process (per event
loop: Playwright objects are bound to the loop that made them), launched lazily with every
signal handler off, because the worker command owns shutdown. Channel: `PDF_BROWSER_CHANNEL`,
else the bundled browser, else `channel="chrome"` on a dev machine without bundled browsers.

**Per render** a fresh context with JavaScript off and every request aborted, so even markup
that slipped past escaping could not fetch a URL or run while the PDF is made; `set_content`,
then `pdf(format="Letter", print_background=True, prefer_css_page_size=True)` (the template's
own `@page` rule owns layout); a 30 s timeout; one relaunch if the browser went away.

**Fitting.** Render normally; if that runs past a page, render compactly and keep the compact
one when it has fewer pages. It changes type size and spacing only, never content: a resume
that spills three lines onto a second page looks careless in a way a slightly smaller font does
not. Page counts come from PDFium (under its lock).

**Dedupe by `render_hash`** = sha256(normal-density HTML + "|" + `RENDER_VERSION`), and for a
tailored resume also + "|" + its download name. Both are known before any render, and the
compact variant is a function of the same resume, so one key covers whichever density the
fitting picks. An existing `resume_pdf` document with the same `(user, render_hash)` is reused
with no render at all. The download name is in a tailored key because the document row carries
it: a tailored resume whose HTML equals the base's (tailoring changed nothing) still downloads as
"… - Resume - <Company>.pdf", not as the base's "… - Resume.pdf". Bump `RENDER_VERSION` whenever
the template, the fitting rule or the PDF options change. Rendering never touches any document's
`text_extracted`.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models.functions import Now
from playwright.async_api import Browser, Playwright, Route, async_playwright
from playwright.async_api import Error as PlaywrightError

from apps.core.db import db
from apps.core.storage import sha256_hex, storage, storage_key
from apps.resumes.models import Document, DocumentKind
from apps.resumes.models import Resume as ResumeRow
from apps.resumes.parse.pdf import PdfReadError, count_pages
from apps.resumes.schema import Resume
from apps.resumes.template import Density, render_resume_html

__all__ = [
    "RENDER_TIMEOUT_MS",
    "RENDER_VERSION",
    "FittedPdf",
    "RenderError",
    "RenderResult",
    "RenderedPdf",
    "close_browser",
    "pdf_filename",
    "render_fitted",
    "render_hash",
    "render_pdf",
    "render_resume",
]

log = logging.getLogger(__name__)

# Bump whenever the template, the fitting rule or the PDF options change: it is part of the key
# a rendered PDF is reused by.
RENDER_VERSION = "1"
RENDER_TIMEOUT_MS = 30_000
# Per half of closing the browser (`browser.close()`, then `playwright.stop()`); a healthy close
# takes well under a second. The two together stay inside the worker's own 10 s bound.
CLOSE_TIMEOUT_S = 4.0

PDF_MIME = "application/pdf"


class RenderError(Exception):
    """A resume that cannot be rendered. The message is shown to the user."""


@dataclass(frozen=True, slots=True)
class RenderedPdf:
    pdf: bytes
    pages: int


@dataclass(frozen=True, slots=True)
class FittedPdf:
    pdf: bytes
    pages: int
    density: Density


@dataclass(frozen=True, slots=True)
class RenderResult:
    document_id: uuid.UUID
    pages: int
    density: Density | None  # None when an earlier render was reused
    reused: bool


# --- The browser ----------------------------------------------------------------------------------


class _Chromium:
    """One lazily launched browser for the running event loop."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._lock: asyncio.Lock | None = None

    def _for_this_loop(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            # A browser started on another loop cannot be used from this one (tests run one
            # loop per test; the worker runs one for its lifetime).
            self._loop, self._playwright, self._browser = loop, None, None
            self._lock = asyncio.Lock()
        assert self._lock is not None
        return self._lock

    async def browser(self) -> Browser:
        async with self._for_this_loop():
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            await self._close_unlocked()
            self._playwright = await async_playwright().start()
            self._browser = await _launch(self._playwright)
            return self._browser

    async def close(self) -> None:
        async with self._for_this_loop():
            await self._close_unlocked()

    async def _close_unlocked(self) -> None:
        """Each half is bounded: a close that never answers (seen live when a second SIGINT
        interrupted it mid-way) must not keep the worker from exiting. `stop()` runs even after
        a stuck `close()`: it ends the driver process, which takes any browser it still has
        with it."""
        browser, playwright = self._browser, self._playwright
        self._browser, self._playwright = None, None
        if browser is not None:
            await _bounded("close the browser", browser.close)
        if playwright is not None:
            await _bounded("stop the Playwright driver", playwright.stop)


async def _bounded(what: str, step: Callable[[], Awaitable[None]]) -> None:
    try:
        async with asyncio.timeout(CLOSE_TIMEOUT_S):
            await step()
    except TimeoutError:
        log.warning("render: could not %s within %ss; going on without it", what, CLOSE_TIMEOUT_S)
    except Exception:
        log.debug("render: could not %s", what, exc_info=True)


_CHROMIUM = _Chromium()

_MISSING_EXECUTABLE = re.compile(
    r"Executable doesn't exist|executable doesn't exist|Failed to launch.*ENOENT", re.IGNORECASE
)
_BROWSER_GONE = re.compile(
    r"has been closed|Target closed|browser has disconnected|Browser closed|crashed",
    re.IGNORECASE,
)


async def _launch(playwright: Playwright) -> Browser:
    """`PDF_BROWSER_CHANNEL` when set; otherwise the bundled Chromium (what the production image
    ships), falling back to the locally installed Chrome when no bundled browser is
    downloaded (a dev machine)."""

    async def launch(channel: str | None) -> Browser:
        # The worker owns shutdown: Playwright's own handlers would close the browser the moment
        # a signal arrives, in the middle of the worker draining its in-flight jobs.
        return await playwright.chromium.launch(
            channel=channel,
            headless=True,
            handle_sigint=False,
            handle_sigterm=False,
            handle_sighup=False,
        )

    channel = (settings.ENV.pdf_browser_channel or "").strip()
    if channel:
        return await launch(channel)
    try:
        return await launch(None)
    except PlaywrightError as error:
        if not _MISSING_EXECUTABLE.search(str(error)):
            raise
        return await launch("chrome")


async def close_browser() -> None:
    """Closes this loop's browser (the worker calls it on shutdown; tests after rendering)."""
    await _CHROMIUM.close()


async def _refuse(route: Route, *_: object) -> None:
    """A resume page loads nothing: every request is aborted."""
    await route.abort()


async def _render_once(html: str) -> bytes:
    browser = await _CHROMIUM.browser()
    context = await browser.new_context(java_script_enabled=False)
    try:
        await context.route("**/*", _refuse)
        page = await context.new_page()
        page.set_default_timeout(RENDER_TIMEOUT_MS)
        await page.set_content(html, wait_until="load")
        return await page.pdf(format="Letter", print_background=True, prefer_css_page_size=True)
    finally:
        with contextlib.suppress(Exception):
            await context.close()


async def render_pdf(html: str) -> RenderedPdf:
    """Renders a complete HTML document to a Letter PDF, relaunching the browser once if it
    went away under us (a crash, an OOM kill, closed elsewhere)."""
    try:
        pdf = await _render_once(html)
    except PlaywrightError as error:
        if not _BROWSER_GONE.search(str(error)):
            raise
        log.warning("render: the browser went away mid-render; relaunching once")
        await _CHROMIUM.close()
        pdf = await _render_once(html)
    return RenderedPdf(pdf=pdf, pages=await asyncio.to_thread(count_pages, pdf))


async def render_fitted(resume: Resume) -> FittedPdf:
    """Normal density, or compact when that saves a page."""
    normal = await render_pdf(render_resume_html(resume))
    if normal.pages > 1:
        compact = await render_pdf(render_resume_html(resume, density="compact"))
        if compact.pages < normal.pages:
            return FittedPdf(pdf=compact.pdf, pages=compact.pages, density="compact")
    return FittedPdf(pdf=normal.pdf, pages=normal.pages, density="normal")


def render_hash(resume: Resume, company: str | None = None) -> str:
    """The key a rendered PDF is reused by: sha256(normal-density HTML | RENDER_VERSION), plus
    `| <download name>` for a resume tailored for `company`."""
    tailored_name = pdf_filename(resume, company) if company is not None else None
    return _hash_html(render_resume_html(resume), tailored_name)


def _hash_html(html_normal: str, tailored_name: str | None = None) -> str:
    text = f"{html_normal}|{RENDER_VERSION}"
    if tailored_name is not None:
        text = f"{text}|{tailored_name}"
    return hashlib.sha256(text.encode()).hexdigest()


_UNSAFE_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_SPACES = re.compile(r"\s+")


def pdf_filename(resume: Resume, company: str | None = None) -> str:
    """The download name: "Dana Reyes - Resume.pdf", or "Dana Reyes - Resume - Stripe.pdf" for a
    tailored one, so several downloads do not all arrive as the same "Resume.pdf"."""

    def safe(value: str | None) -> str:
        return _SPACES.sub(" ", _UNSAFE_FILENAME.sub("", value or "")).strip()

    name, target = safe(resume.basics.name), safe(company)
    base = f"{name} - Resume" if name else "Resume"
    return f"{base} - {target}.pdf" if target else f"{base}.pdf"


# --- A stored resume to a stored PDF --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Loaded:
    resume: Resume
    company: str | None


def _load(user_id: str, resume_id: uuid.UUID | str) -> _Loaded:
    row = (
        ResumeRow.objects.for_user(user_id)
        .select_related("match__job")
        .filter(id=resume_id)
        .first()
    )
    if row is None:
        raise RenderError("That resume no longer exists.")
    resume = Resume.read(row.data)
    if resume is None:
        raise RenderError("The stored resume could not be read.")
    # A tailored resume is named for the company it was written for.
    company = row.match.job.company_name if row.kind == "tailored" and row.match else None
    return _Loaded(resume=resume, company=company)


def _find_rendered(user_id: str, key: str) -> Document | None:
    return Document.objects.for_user(user_id).filter(render_hash=key).first()


def _point_at(user_id: str, resume_id: uuid.UUID | str, document_id: uuid.UUID) -> None:
    ResumeRow.objects.for_user(user_id).filter(id=resume_id).update(
        rendered_document_id=document_id, updated_at=Now()
    )


def _record(
    user_id: str,
    resume_id: uuid.UUID | str,
    *,
    key: str,
    filename: str,
    path: str,
    pdf: bytes,
) -> tuple[Document, bool]:
    """Stores the document row and points the resume at it, in one transaction. Returns the
    document and whether it is the one just written (False: another render of the same resume
    got there first, and its document is used instead)."""
    try:
        with transaction.atomic():
            document = Document.objects.create_for(
                user_id,
                kind=DocumentKind.RESUME_PDF,
                filename=filename,
                mime=PDF_MIME,
                size_bytes=len(pdf),
                storage_path=path,
                sha256=sha256_hex(pdf),
                render_hash=key,
            )
            _point_at(user_id, resume_id, document.id)
            return document, True
    except IntegrityError:
        existing = _find_rendered(user_id, key)
        if existing is None:
            # Byte-identical to another document of this user (`documents_user_sha256`): a
            # tailored resume rendered within the same second as its identical base, since
            # Chromium's PDFs of one HTML differ only in their creation second. One row per
            # bytes, so that one is used; the next render gets its own.
            existing = Document.objects.for_user(user_id).filter(sha256=sha256_hex(pdf)).first()
        if existing is None:
            raise
        _point_at(user_id, resume_id, existing.id)
        return existing, False


def _repair(
    user_id: str, resume_id: uuid.UUID | str, document_id: uuid.UUID, *, path: str, pdf: bytes
) -> None:
    """Points a document whose file went missing at a fresh render of the same HTML, and the
    resume at the document, in one transaction (one outcome to wait for, see `_settled`)."""
    with transaction.atomic():
        Document.objects.for_user(user_id).filter(id=document_id).update(
            storage_path=path, size_bytes=len(pdf), sha256=sha256_hex(pdf)
        )
        _point_at(user_id, resume_id, document_id)


async def _settled[T](path: str, keeps_file: Callable[[T], bool], write: Awaitable[T]) -> T:
    """Awaits a `db()` write that records the file just stored at `path`, and deletes that file
    unless the write committed and `keeps_file(result)` says the row uses it.

    A cancel (Stop, a step timeout, a shutdown abort) cannot stop the write's thread, and asgiref
    stops waiting for it at once: the write may still commit after the cancel arrived. Deleting
    the file then left a committed document whose file was gone (a broken PDF link). So on a
    cancel this waits, shielded, for the write's real outcome before judging the file, and only
    then lets the cancel through. An outcome it cannot learn (cancelled again while waiting)
    keeps the file: an orphan file is harmless, a row without its file is not."""
    task = asyncio.ensure_future(write)
    try:
        result = await asyncio.shield(task)
    except asyncio.CancelledError:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait({task})
        if task.done() and (
            task.cancelled() or task.exception() is not None or not keeps_file(task.result())
        ):
            await asyncio.to_thread(storage().delete, path)
        raise
    except BaseException:
        await asyncio.to_thread(storage().delete, path)  # nothing committed: no orphan file
        raise
    if not keeps_file(result):
        await asyncio.to_thread(storage().delete, path)
    return result


def _read_stored(path: str) -> bytes | None:
    try:
        with storage().open(path) as handle:
            return handle.read()
    except FileNotFoundError:
        return None


async def render_resume(user_id: str, resume_id: uuid.UUID | str) -> RenderResult:
    """Renders a stored resume (base or tailored) to PDF, stores it as a `resume_pdf` document
    and points `resumes.rendered_document` at it, unless a document with the same render hash
    exists: then that one is reused and nothing is rendered."""
    loaded = await db(_load, user_id, resume_id)
    filename = pdf_filename(loaded.resume, loaded.company)
    key = _hash_html(
        render_resume_html(loaded.resume), filename if loaded.company is not None else None
    )

    existing = await db(_find_rendered, user_id, key)
    if existing is not None:
        stored = await asyncio.to_thread(_read_stored, existing.storage_path)
        if stored is not None:
            try:
                pages = await asyncio.to_thread(count_pages, stored)
            except PdfReadError:
                pages = None
            if pages is not None:
                await db(_point_at, user_id, resume_id, existing.id)
                return RenderResult(document_id=existing.id, pages=pages, density=None, reused=True)

    fitted = await render_fitted(loaded.resume)
    path = storage_key(user_id, DocumentKind.RESUME_PDF.value, filename)
    await asyncio.to_thread(storage().put, path, fitted.pdf)

    if existing is not None:  # its file was missing or unreadable: repair it in place
        await _settled(
            path,
            lambda _: True,
            db(_repair, user_id, resume_id, existing.id, path=path, pdf=fitted.pdf),
        )
        return RenderResult(
            document_id=existing.id, pages=fitted.pages, density=fitted.density, reused=False
        )

    document, written = await _settled(
        path,
        lambda recorded: recorded[1],  # False: another render's document is used instead
        db(_record, user_id, resume_id, key=key, filename=filename, path=path, pdf=fitted.pdf),
    )
    if not written:
        return RenderResult(document_id=document.id, pages=fitted.pages, density=None, reused=True)
    return RenderResult(
        document_id=document.id, pages=fitted.pages, density=fitted.density, reused=False
    )
