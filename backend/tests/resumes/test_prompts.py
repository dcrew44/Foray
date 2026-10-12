"""The §8.10 prompt goldens for this app's builders, byte for byte: resume extraction (text path,
and the vision path's text parts), tailoring and the verify judge.

ALLOWED_DIFFS: none in the builders. The extraction prompt embeds the document's text, and for
`serif-dense.pdf` PDFium's text differs from pdf.js's in the lines `test_pdf_text.py` declares;
so the builder is pinned on the TS text for every fixture, and end to end (our parser, our
builder) for the fixtures whose text is identical.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from typing import Any

import pytest

from apps.accounts.schemas import Preferences
from apps.resumes import extract, tailor, verify
from apps.resumes.extract import (
    INSTRUCTIONS,
    VISION_INSTRUCTIONS,
    DocumentType,
    extract_from,
    read_document,
    text_prompt,
    vision_prompt,
)
from apps.resumes.parse.pdf import render_pages
from apps.resumes.schema import TailorChange
from apps.resumes.tailor import build_tailor_prompt, evaluation_notes
from apps.resumes.verify import verify_tailored
from tests.resumes.support import fixture_bytes, golden, resume

EXTRACT_TEXT_SOURCES: dict[str, tuple[str, DocumentType, bool]] = {
    "single-column": ("resumes/pdf/single-column.json", "pdf", True),
    "two-column": ("resumes/pdf/two-column.json", "pdf", True),
    "serif-dense": ("resumes/pdf/serif-dense.json", "pdf", False),  # text differs, see above
    "plain-docx": ("resumes/docx/plain.json", "docx", True),
}


@pytest.mark.parametrize("case", list(EXTRACT_TEXT_SOURCES))
async def test_extract_text_prompt(case: str):
    expected = golden(f"prompts/extract_text/{case}.json")
    text_golden, kind, identical_text = EXTRACT_TEXT_SOURCES[case]
    ts_text = golden(text_golden)["text"]

    assert text_prompt(ts_text) == (expected["instructions"], expected["prompt"])
    assert expected["instructions"] == INSTRUCTIONS

    if identical_text:
        parsed = await read_document(fixture_bytes(expected["input"]["fixture"]), kind)
        assert text_prompt(parsed.text) == (expected["instructions"], expected["prompt"])


def _describe(part: Any) -> dict[str, Any]:
    if isinstance(part, str):
        return {"type": "text", "text": part}
    return {"type": "file", "mediaType": part.media_type}


def test_extract_vision_prompt():
    expected = golden("prompts/extract_vision/single-column-forced.json")
    images = render_pages(fixture_bytes("resumes/single-column.pdf"))
    instructions, parts = vision_prompt(images)
    assert instructions == VISION_INSTRUCTIONS == expected["instructions"]
    assert [_describe(part) for part in parts] == expected["parts"]
    assert "".join(part for part in parts if isinstance(part, str)) == expected["prompt"]


class Captured(Exception):
    pass


async def test_extract_from_sends_exactly_the_golden_prompts(monkeypatch: pytest.MonkeyPatch):
    """The wiring: `extract_from` asks the `extract` purpose with these very prompts."""
    calls: list[dict[str, Any]] = []

    async def capture(purpose: str, output: type, **kwargs: Any) -> Any:
        calls.append({"purpose": purpose, "output": output, **kwargs})
        raise Captured

    monkeypatch.setattr(extract, "structured", capture)
    data = fixture_bytes("resumes/single-column.pdf")
    parsed = await read_document(data, "pdf")

    with pytest.raises(Captured):
        await extract_from(parsed, data, "pdf")
    text_golden = golden("prompts/extract_text/single-column.json")
    assert calls[-1]["purpose"] == "extract"
    assert calls[-1]["output"].__name__ == "ExtractedResume"
    assert (calls[-1]["instructions"], calls[-1]["prompt"]) == (
        text_golden["instructions"],
        text_golden["prompt"],
    )

    with pytest.raises(Captured):
        await extract_from(parsed, data, "pdf", force_vision=True)
    vision_golden = golden("prompts/extract_vision/single-column-forced.json")
    assert calls[-1]["instructions"] == vision_golden["instructions"]
    assert [_describe(part) for part in calls[-1]["prompt"]] == vision_golden["parts"]


@dataclass
class Job:
    title: str
    company_name: str
    locations: list[str] = field(default_factory=list)
    remote: str = "unknown"
    description_text: str | None = None
    description_raw: str | None = None


# What TS `htmlToText` returned for the one HTML-only posting (it is in that golden's prompt).
TS_HTML_TO_TEXT = {
    "&lt;p&gt;Données &amp; SQL&lt;/p&gt;<ul><li>Tableaux</li></ul>": (
        "<p>Données & SQL</p>\n\n• Tableaux"
    ),
}


@pytest.mark.parametrize(
    "case", ["with_notes_and_goals", "html_only_no_notes", "no_description_fast_only"]
)
def test_tailor_prompt(case: str, monkeypatch: pytest.MonkeyPatch):
    expected = golden(f"prompts/tailor/{case}.json")
    data = expected["input"]
    job_data = data["job"]
    if importlib.util.find_spec("apps.jobs.sources") is None:
        # Step 3C's cleaner (`jobs/sources/clean.py`) is not in this tree yet: stand in with the
        # TS output for this input, so the rest of the builder is still pinned byte for byte.
        monkeypatch.setattr(tailor, "_html_to_text", TS_HTML_TO_TEXT.__getitem__)
    job = Job(
        title=job_data["title"],
        company_name=job_data["companyName"],
        locations=job_data["locations"],
        remote=job_data["remote"],
        description_text=job_data["descriptionText"],
        description_raw=job_data["descriptionRaw"],
    )
    match = data["match"] or {}
    preferences = Preferences.model_validate(data["preferences"]) if data["preferences"] else None
    instructions, prompt = build_tailor_prompt(
        resume(data["base"]),
        job,
        evaluation_notes(match.get("deepEval"), match.get("fastEval")),
        preferences,
    )
    assert instructions == expected["instructions"]
    assert prompt == expected["prompt"]


@pytest.mark.parametrize("case", ["work_summary_project_with_changes", "education_without_changes"])
async def test_verify_judge_prompt(case: str, monkeypatch: pytest.MonkeyPatch):
    """Only lines that pass the rules reach the judge, so the prompt comes from the whole
    `verify_tailored` path with the default model judge."""
    expected = golden(f"prompts/verify_judge/{case}.json")
    data = expected["input"]
    seen: dict[str, Any] = {}

    async def capture(purpose: str, output: type, **kwargs: Any) -> Any:
        seen.update(purpose=purpose, **kwargs)
        raise Captured

    monkeypatch.setattr(verify, "structured", capture)
    result = await verify_tailored(
        resume(data["base"]),
        resume(data["tailored"]),
        [TailorChange.model_validate(change) for change in data["changes"]],
    )
    assert seen["purpose"] == "verify"
    assert seen["instructions"] == expected["instructions"]
    assert seen["prompt"] == expected["prompt"]
    # The capture raised, so the verifier reverted every rewrite it could not check.
    assert result.model_error is not None
    assert all(finding.source == "model" for finding in result.findings)
