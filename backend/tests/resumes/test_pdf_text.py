"""PDF and DOCX text (design §11.2, §12.3): the fixtures against the pdf.js/mammoth goldens, the
garble decisions, page images, error wording, and the PDFium lock under stress.

ALLOWED_DIFFS for `serif-dense.pdf` (§11.2's accepted parity gaps): PDFium reads its 0.11-0.17 em
letter-spaced name line as words where pdf.js split every glyph, spaces one letter-spaced
heading differently, and keeps the small-caps skill labels in source case. The garble decision
is the same.
"""

from __future__ import annotations

import hashlib
import struct
import threading
import zlib
from concurrent.futures import ThreadPoolExecutor

import pytest

from apps.resumes.parse.docx import DocxReadError, parse_docx
from apps.resumes.parse.pdf import PdfReadError, count_pages, parse_pdf, render_pages
from tests.resumes.support import fixture_bytes, golden

EXACT = ["single-column", "two-column"]

SERIF_DENSE_ALLOWED = {
    # line in pdf.js (TS) -> line from PDFium
    "I L O N A B E R G Q U I S T- M A H O N E Y": "ILONA BERGQUI ST-MAHONEY",
    "E D U C A T I O N": "E D U C AT I O N",
    "LANGUAGES — C++17/20, Python, Julia, Rust (intermediate), SQL, Fortran (legacy maintenance), "
    "LaTeX": "Languages — C++17/20, Python, Julia, Rust (intermediate), SQL, Fortran (legacy "
    "maintenance), LaTeX",
    "SCIENTIFIC — NumPy, SciPy, Eigen, pybind11, OpenMP, MPI, CUDA (basic), BLAS/LAPACK": (
        "Scientific — NumPy, SciPy, Eigen, pybind11, OpenMP, MPI, CUDA (basic), BLAS/LAPACK"
    ),
    "DATA & WORKFLOW — Apache Airflow, Parquet, DuckDB, PostgreSQL, dbt, Snakemake": (
        "Data & workflow — Apache Airflow, Parquet, DuckDB, PostgreSQL, dbt, Snakemake"
    ),
    "TOOLING — CMake, Git, Docker, Slurm, GitHub Actions, perf, Valgrind, Google Benchmark": (
        "Tooling — CMake, Git, Docker, Slurm, GitHub Actions, perf, Valgrind, Google Benchmark"
    ),
}


@pytest.mark.parametrize("name", EXACT)
def test_pdf_text_matches_pdfjs_byte_for_byte(name: str):
    expected = golden(f"resumes/pdf/{name}.json")
    parsed = parse_pdf(fixture_bytes(f"resumes/{name}.pdf"))
    assert parsed.text == expected["text"]
    assert parsed.page_count == expected["pageCount"]
    assert parsed.garble.dump() == expected["garble"]


def test_serif_dense_differs_only_where_declared():
    expected = golden("resumes/pdf/serif-dense.json")
    parsed = parse_pdf(fixture_bytes("resumes/serif-dense.pdf"))
    assert parsed.page_count == expected["pageCount"]
    ts_lines = [SERIF_DENSE_ALLOWED.get(line, line) for line in expected["text"].split("\n")]
    assert parsed.text.split("\n") == ts_lines
    assert parsed.garble.garbled is expected["garble"]["garbled"] is False
    assert parsed.garble.reasons == expected["garble"]["reasons"]


def test_docx_text_matches_mammoth_js():
    expected = golden("resumes/docx/plain.json")
    parsed = parse_docx(fixture_bytes("resumes/plain.docx"))
    assert parsed.text == expected["text"]
    assert parsed.page_count == 1
    assert parsed.garble.dump() == expected["garble"]


def test_unreadable_files_get_plain_words():
    with pytest.raises(PdfReadError, match=r"^Invalid PDF structure\.$"):
        parse_pdf(b"%PDF-1.7 this is not really a PDF")
    with pytest.raises(PdfReadError, match=r"^Invalid PDF structure\.$"):
        parse_pdf(fixture_bytes("resumes/plain.docx"))
    with pytest.raises(PdfReadError):
        parse_pdf(fixture_bytes("resumes/single-column.pdf")[:2000])
    with pytest.raises(DocxReadError, match=r"^That Word file could not be read\.$"):
        parse_docx(fixture_bytes("resumes/single-column.pdf"))


def _png_size(png: bytes) -> tuple[int, int]:
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", png[16:24])
    return width, height


def test_page_images_for_the_vision_fallback():
    images = render_pages(fixture_bytes("resumes/single-column.pdf"))
    assert len(images) == 2  # every page, up to four
    assert [_png_size(image) for image in images] == [(1224, 1584), (1224, 1584)]
    # A valid PNG: its IDAT inflates to one filter byte plus RGB per row.
    length = struct.unpack(">I", images[0][33:37])[0]
    raw = zlib.decompress(images[0][41 : 41 + length])
    assert len(raw) == 1584 * (1 + 1224 * 3)
    assert render_pages(fixture_bytes("resumes/single-column.pdf"), max_pages=1) == images[:1]


def test_count_pages():
    assert count_pages(fixture_bytes("resumes/two-column.pdf")) == 2


def _fingerprint(data: bytes) -> str:
    parsed = parse_pdf(data)
    thumbnail = render_pages(data, max_pages=1, scale=0.2)[0]
    return hashlib.sha256(parsed.text.encode() + thumbnail).hexdigest()


def test_pdfium_lock_stress():
    """8 threads x 200 calls of text extraction plus a page render, each on its own document,
    must equal a serial baseline: unlocked, PDFium crashes or returns wrong text
    (`research/documents-pdf.md`, Verification 1)."""
    names = ["single-column", "two-column", "serif-dense"]
    documents = {name: fixture_bytes(f"resumes/{name}.pdf") for name in names}
    baseline = {name: _fingerprint(data) for name, data in documents.items()}
    mismatches: list[str] = []
    guard = threading.Lock()

    def worker(seed: int) -> None:
        for call in range(200):
            name = names[(seed + call) % len(names)]
            if _fingerprint(documents[name]) != baseline[name]:
                with guard:
                    mismatches.append(f"{seed}:{call}:{name}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        for future in [pool.submit(worker, seed) for seed in range(8)]:
            future.result()
    assert mismatches == []
