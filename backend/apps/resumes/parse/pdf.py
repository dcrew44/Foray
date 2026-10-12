"""PDF text and page images, with pypdfium2 (design §11.2; replaces `src/lib/resume/parse/pdf.ts`).

Text first, pictures only if the text is unusable: reading the text layer is free and a vision
call is not, so every PDF is read as text, measured (`garble.detect_garble`), and rendered to
images only when the measurement says the text cannot be trusted.

**One process-wide lock covers every PDFium call, `close()` included.** PDFium is not
thread-safe, not even across different documents: unlocked concurrent use crashes the
interpreter or silently returns wrong text (`research/documents-pdf.md`, Verification 1). Every
object is closed explicitly inside the lock, so no garbage-collector finalizer runs a PDFium call
on another thread. The calls are short (about 3 ms of text per resume, 15 ms per rendered page),
so the lock costs little. The API runs these in `db()`-free threads (`asyncio.to_thread`).

**Text** is `get_text_range()` per page, with pdf.js's selective NFKC normalisation and its
hyphen mapping, then unpdf's merge rules: pages joined by a newline, `[^\\S\\n]+` to one space,
` ?\\n ?` to a newline, three or more newlines to two. That matched pdf.js byte for byte on the
single- and two-column fixtures.
"""

from __future__ import annotations

import re
import struct
import threading
import unicodedata
import zlib
from dataclasses import dataclass

import pypdfium2 as pdfium

from apps.resumes.parse.garble import JS_WHITESPACE, GarbleReport, detect_garble

__all__ = [
    "MAX_VISION_PAGES",
    "PDFIUM_LOCK",
    "ParsedDocument",
    "PdfReadError",
    "count_pages",
    "parse_pdf",
    "render_pages",
]

PDFIUM_LOCK = threading.Lock()

# How many pages a vision model is shown.
MAX_VISION_PAGES = 4
# Scale 2 is the cheapest setting at which 9-10 pt body text stays legible (1224x1584 for a
# Letter page); below that the model starts inventing plausible-looking dates.
VISION_SCALE = 2


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """A document's text, its page count, and whether the text can be trusted."""

    text: str
    page_count: int
    garble: GarbleReport


class PdfReadError(Exception):
    """The file is not a PDF PDFium can read. The message is shown to the user."""


# pdf.js `normalizeUnicode` (6.1.200, as bundled by unpdf 1.8.1): NFKC on a fixed set of
# compatibility code points only (NBSP, U+2000-U+200A, ligatures, Arabic presentation forms...).
_PDFJS_NFKC = re.compile(
    "([\u00a0\u00b5\u037e\u0eb3\u2000-\u200a\u202f\u2126\ufb00-\ufb04\ufb06\ufb20-\ufb36"
    "\ufb38-\ufb3c\ufb3e\ufb40\ufb41\ufb43\ufb44\ufb46-\ufba1\ufba4-\ufba9\ufbae-\ufbb1"
    "\ufbd3-\ufbdc\ufbde-\ufbe7\ufbea-\ufbf8\ufbfc\ufbfd\ufc00-\ufc5d\ufc64-\ufcf1"
    "\ufcf5-\ufd3d\ufd88\ufdf4\ufdfa\ufdfb\ufe71\ufe77\ufe79\ufe7b\ufe7d]+)|(\ufb05+)"
)
# pdf.js turns the hyphen variants U+2010 and U+2011 into "-" (measured).
_HYPHENS = str.maketrans({"\u2010": "-", "\u2011": "-"})
# unpdf's merge: `[^\S\n]+` with JavaScript's `\s`.
_NON_NEWLINE_SPACE = re.compile(f"[{JS_WHITESPACE.replace(chr(10), '')}]+")
_NEWLINE_PADDING = re.compile(r" ?\n ?")
_BLANK_LINES = re.compile(r"\n{3,}")


def _pdfjs_normalize(text: str) -> str:
    text = _PDFJS_NFKC.sub(
        lambda m: (
            unicodedata.normalize("NFKC", m.group(1)) if m.group(1) else "\u017ft" * len(m[2])
        ),
        text,
    )
    return text.translate(_HYPHENS)


def _merge_pages(pages: list[str]) -> str:
    text = "\n".join(pages)
    text = _NON_NEWLINE_SPACE.sub(" ", text)
    text = _NEWLINE_PADDING.sub("\n", text)
    return _BLANK_LINES.sub("\n\n", text)


def _readable(error: pdfium.PdfiumError) -> PdfReadError:
    """PDFium's errors in the words pdf.js used, or plain ones (design §11.2)."""
    code = getattr(error, "err_code", None)
    if code == pdfium.raw.FPDF_ERR_PASSWORD:
        return PdfReadError("The PDF is password-protected.")
    if code == pdfium.raw.FPDF_ERR_SECURITY:
        return PdfReadError("The PDF uses an unsupported security scheme.")
    if code in (pdfium.raw.FPDF_ERR_FORMAT, pdfium.raw.FPDF_ERR_FILE):
        return PdfReadError("Invalid PDF structure.")
    return PdfReadError("The PDF could not be read.")


def _open(data: bytes) -> pdfium.PdfDocument:
    """Opens a document. The caller holds the lock and closes it."""
    try:
        return pdfium.PdfDocument(data)
    except pdfium.PdfiumError as error:
        raise _readable(error) from error


def _page_texts(data: bytes) -> list[str]:
    with PDFIUM_LOCK:
        document = _open(data)
        try:
            texts: list[str] = []
            for index in range(len(document)):
                page = document[index]
                try:
                    textpage = page.get_textpage()
                    try:
                        text = textpage.get_text_range()
                    finally:
                        textpage.close()
                finally:
                    page.close()
                # pdf.js ends a page without trailing whitespace; PDFium sometimes ends the last
                # line with CRLF (Quartz output).
                texts.append(_pdfjs_normalize(text).rstrip())
            return texts
        except pdfium.PdfiumError as error:
            raise _readable(error) from error
        finally:
            document.close()


def parse_pdf(data: bytes) -> ParsedDocument:
    """The PDF's text, merged as unpdf did, with its page count and garble report."""
    pages = _page_texts(data)
    text = _merge_pages(pages)
    return ParsedDocument(text=text, page_count=len(pages), garble=detect_garble(text, len(pages)))


def count_pages(data: bytes) -> int:
    """The number of pages, read by PDFium (replaces the `/Type /Page` regex)."""
    with PDFIUM_LOCK:
        document = _open(data)
        try:
            return len(document)
        finally:
            document.close()


def render_pages(
    data: bytes, max_pages: int = MAX_VISION_PAGES, scale: float = VISION_SCALE
) -> list[bytes]:
    """The first `max_pages` pages as PNGs, for the vision fallback."""
    images: list[bytes] = []
    with PDFIUM_LOCK:
        document = _open(data)
        try:
            for index in range(min(len(document), max_pages)):
                page = document[index]
                try:
                    bitmap = page.render(scale=scale, rev_byteorder=True)  # pyright: ignore[reportArgumentType]
                    try:
                        images.append(
                            _png(
                                bytes(bitmap.buffer),
                                bitmap.width,
                                bitmap.height,
                                bitmap.stride,
                                bitmap.n_channels,
                            )
                        )
                    finally:
                        bitmap.close()
                finally:
                    page.close()
        except pdfium.PdfiumError as error:
            raise _readable(error) from error
        finally:
            document.close()
    return images


def _png(raw: bytes, width: int, height: int, stride: int, channels: int) -> bytes:
    """A minimal PNG encoder (8-bit RGB or grey, no filtering), so rendering needs no Pillow."""
    color_type = {1: 0, 3: 2, 4: 6}[channels]
    row = width * channels
    scanlines = b"".join(
        b"\x00" + raw[offset : offset + row] for offset in range(0, height * stride, stride)
    )

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body))
            + kind
            + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(scanlines, 6))
        + chunk(b"IEND", b"")
    )
