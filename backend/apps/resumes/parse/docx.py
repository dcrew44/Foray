"""DOCX text, with mammoth (design §11.2; a port of `src/lib/resume/parse/docx.ts`).

DOCX is structured text, so there is no image fallback: if mammoth cannot read it, the file is
not a DOCX and the user should be told so rather than charged for a vision call that would also
fail. `mammoth.extract_raw_text` is pure Python and thread-safe, so it needs no lock.
"""

from __future__ import annotations

import io

import mammoth

from apps.resumes.parse.garble import detect_garble
from apps.resumes.parse.pdf import ParsedDocument

__all__ = ["DocxReadError", "parse_docx"]


class DocxReadError(Exception):
    """The file is not a Word document mammoth can read. The message is shown to the user."""


def parse_docx(data: bytes) -> ParsedDocument:
    try:
        result = mammoth.extract_raw_text(io.BytesIO(data))
    except Exception as error:  # a bad zip, malformed XML, a missing part: many kinds
        raise DocxReadError("That Word file could not be read.") from error
    text: str = result.value
    # Word documents have no page count until they are laid out. Treating the document as one
    # page only affects the characters-per-page signal, which is about scans and cannot apply.
    return ParsedDocument(text=text, page_count=1, garble=detect_garble(text, 1))
