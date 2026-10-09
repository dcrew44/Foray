"""What an upload really is, and how big it may be (design §11.1, §5.2).

**The size cap.** `MaxSizeUploadHandler` is the first of `FILE_UPLOAD_HANDLERS`. It counts each
file's bytes; past 10 MB it stops passing data to the in-memory handler behind it (so nothing
larger is ever held), and when the file ends it refuses it with its real size, as today: "That
file is 12.3 MB; the limit is 10 MB." (413). Under ASGI Django has already spooled the request
body to a temporary file, so counting to the end costs no extra network.

**Magic bytes, not the client's MIME.** `sniff(data)` reads the bytes themselves: `%PDF-` is a
PDF, and a ZIP holding `[Content_Types].xml` and `word/document.xml` is a Word document.
Anything else is refused (415 "Upload a PDF or a Word (.docx) file.") before it is stored or
billed. A renamed `.exe` is refused whatever its name or `Content-Type` say.

**The DOCX guard.** A Word file is a ZIP, and a ZIP can declare far more than it holds: mammoth
would inflate a bomb in the worker. A file whose entries declare more than 50 MB uncompressed,
or that has more than 2,000 entries, is refused (415 "That Word file could not be read.").
Python's `zipfile` never inflates an entry past its declared size, so the declared sizes bound
what mammoth can be made to read.

`UploadRejected` carries the status and the sentence; the API turns it into its envelope, and
the import run (which sniffs the stored bytes again) into a run failure.
"""

from __future__ import annotations

import io
import zipfile
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from django.core.files.uploadhandler import FileUploadHandler

__all__ = [
    "DOCX_MIME",
    "MAX_DOCX_ENTRIES",
    "MAX_DOCX_UNCOMPRESSED",
    "MAX_UPLOAD_BYTES",
    "MIME",
    "PDF_MIME",
    "UNREADABLE_WORD",
    "UNSUPPORTED",
    "DocumentType",
    "MaxSizeUploadHandler",
    "UploadRejected",
    "sniff",
    "too_large_message",
]

DocumentType = Literal["pdf", "docx"]

# 10 MB. A resume that large is a scan, and scans go through the vision path anyway.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_DOCX_UNCOMPRESSED = 50 * 1024 * 1024
MAX_DOCX_ENTRIES = 2_000

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MIME: dict[DocumentType, str] = {"pdf": PDF_MIME, "docx": DOCX_MIME}

UNSUPPORTED = "Upload a PDF or a Word (.docx) file."
UNREADABLE_WORD = "That Word file could not be read."

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
_DOCX_PARTS = frozenset({"[Content_Types].xml", "word/document.xml"})
_MB = Decimal(1024 * 1024)


class UploadRejected(Exception):
    """An upload Foray will not take: the status to answer and the sentence to show."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def too_large_message(size: int) -> str:
    """Today's words, with the size as `(size / 1024 / 1024).toFixed(1)` printed it (halves
    round up)."""
    megabytes = (Decimal(size) / _MB).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return f"That file is {megabytes} MB; the limit is 10 MB."


class MaxSizeUploadHandler(FileUploadHandler):
    """Refuses a file over `MAX_UPLOAD_BYTES` with 413 and its real size (see the module
    docstring). It stores nothing itself: the handler after it keeps the bytes of a file that
    fits."""

    too_large = False

    def new_file(
        self,
        field_name: str,
        file_name: str,
        content_type: str,
        content_length: int | None,
        charset: str | None = None,
        content_type_extra: dict[str, Any] | None = None,
    ) -> None:
        super().new_file(
            field_name, file_name, content_type, content_length, charset, content_type_extra
        )
        self.too_large = False

    def receive_data_chunk(self, raw_data: bytes, start: int) -> bytes | None:
        if start + len(raw_data) > MAX_UPLOAD_BYTES:
            self.too_large = True
        # None stops the chunk here: the in-memory handler never holds more than the cap.
        return None if self.too_large else raw_data

    def file_complete(self, file_size: int) -> None:
        # `file_size` counts every byte of the file, including the ones not passed on.
        if self.too_large or file_size > MAX_UPLOAD_BYTES:
            raise UploadRejected(413, too_large_message(file_size))
        return None


def _check_docx(entries: list[zipfile.ZipInfo]) -> None:
    if len(entries) > MAX_DOCX_ENTRIES:
        raise UploadRejected(415, UNREADABLE_WORD)
    if sum(entry.file_size for entry in entries) > MAX_DOCX_UNCOMPRESSED:
        raise UploadRejected(415, UNREADABLE_WORD)


def sniff(data: bytes) -> DocumentType:
    """The document type the bytes say, or `UploadRejected` (415)."""
    if data.startswith(_PDF_MAGIC):
        return "pdf"
    if data.startswith(_ZIP_MAGIC):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
        except zipfile.BadZipFile, zipfile.LargeZipFile, OSError, ValueError, EOFError:
            raise UploadRejected(415, UNSUPPORTED) from None
        names = {entry.filename for entry in entries}
        if names >= _DOCX_PARTS:
            _check_docx(entries)
            return "docx"
    raise UploadRejected(415, UNSUPPORTED)
