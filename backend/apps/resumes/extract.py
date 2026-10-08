"""A document in, a structured resume out (design §11.1, §11.2; a port of
`src/lib/resume/extract.ts`).

The one rule that matters here is that nothing may be invented. A resume is the factual basis
for every later claim the product makes on the user's behalf (tailored bullets cite its items by
id), so a plausible guess is worse than an omission.

The upload run (`pipeline.py`, step 5B) goes through three phases, so this module offers them
separately: `read_document` (reading: text, page count, garble, in a thread, PDFium under its
lock), `extract_from` (extracting: one `extract` call, on the text or, for a garbled PDF, on the
first page images) and `with_ids` (saving assigns ids and provenance). `extract_resume` runs the
first two for callers that want both.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic_ai.messages import BinaryContent, UserContent

from apps.llm.structured import structured
from apps.resumes.parse.docx import DocxReadError, parse_docx
from apps.resumes.parse.garble import GarbleReport
from apps.resumes.parse.pdf import ParsedDocument, PdfReadError, parse_pdf, render_pages
from apps.resumes.schema import ExtractedResume, ItemSource, Resume, with_ids

__all__ = [
    "INSTRUCTIONS",
    "VISION_INSTRUCTIONS",
    "DocumentType",
    "Extraction",
    "ExtractionResult",
    "ResumeReadError",
    "extract_from",
    "extract_resume",
    "read_document",
    "text_prompt",
    "vision_prompt",
]

log = logging.getLogger(__name__)

DocumentType = Literal["pdf", "docx"]

INSTRUCTIONS = """You transcribe resumes into structured data. You are a transcriber, not an editor.

Rules, in order of importance:
1. Never invent. If a field is not in the document, leave it null or omit the
   item. Do not infer an end date from context, do not guess a degree from a
   university name, do not expand an abbreviation you are not certain of.
2. Copy text verbatim. Bullets, job titles, company names and dates appear
   exactly as written — including "Present", "2021 – 2023", and any unusual
   capitalisation. Do not rewrite, summarise, shorten or improve wording.
3. Keep dates in the document's own format. Do not normalise "June 2024" to
   "2024-06".
4. The text may be in the wrong reading order. PDF extraction interleaves
   multi-column layouts, so a line from a sidebar can appear in the middle of a
   job description. Reassemble by meaning: a bullet belongs to the job it
   describes, a skill to the skills list. Do not drop content because it
   appeared in an odd place.
5. Skills keep the document's own grouping in `category` when it has one
   (e.g. "Languages", "Tools"). Never invent a grouping.
6. A line that is a section heading ("EXPERIENCE", "EDUCATION") is structure,
   not content — do not emit it as a bullet or a skill.
7. Letter-spaced text extracts one glyph at a time. "S U M M A R Y" is the
   heading "SUMMARY", and "I L O N A  B E R G Q U I S T" is the name "Ilona
   Bergquist". Close the gaps when a run of single characters clearly spells
   words. This applies to headings and names, not to genuine acronyms or
   initials — "B S" in a degree line is "BS", but "A P I" is "API".
8. Justified text is hyphenated at line breaks, so a word may arrive split as
   "ap-
plied". Rejoin it into "applied". Keep the hyphen only where the word
   is genuinely hyphenated, as in "full-stack" or "event-driven"."""

# The numbering repeats "7." exactly as today's prompt does (pinned by the §8.10 goldens).
VISION_INSTRUCTIONS = f"""{INSTRUCTIONS}
7. You are reading page images because the document's text layer was unusable.
   Read what is visibly printed. If a character is genuinely illegible, omit
   the item rather than guessing at it."""

VISION_PROMPT = "Transcribe this resume."


class ResumeReadError(Exception):
    """The file could not be read. The message is plain words, shown to the user as
    "Could not read that resume: <message>"."""


def text_prompt(text: str) -> tuple[str, str]:
    """`(instructions, prompt)` for the text path."""
    return INSTRUCTIONS, f"Transcribe this resume.\n\n<resume>\n{text}\n</resume>"


def vision_prompt(images: Sequence[bytes]) -> tuple[str, list[UserContent]]:
    """`(instructions, prompt parts)` for the vision path: the request, then one PNG per page."""
    parts: list[UserContent] = [VISION_PROMPT]
    parts.extend(BinaryContent(image, media_type="image/png") for image in images)
    return VISION_INSTRUCTIONS, parts


async def read_document(data: bytes, kind: DocumentType) -> ParsedDocument:
    """The document's text, page count and garble report, read off the event loop."""
    parse = parse_pdf if kind == "pdf" else parse_docx
    try:
        return await asyncio.to_thread(parse, data)
    except (PdfReadError, DocxReadError) as error:
        raise ResumeReadError(str(error)) from error


@dataclass(frozen=True, slots=True)
class Extraction:
    extracted: ExtractedResume
    used_vision: bool  # the text layer was unusable (or forced), so page images were read


async def extract_from(
    parsed: ParsedDocument, data: bytes, kind: DocumentType, *, force_vision: bool = False
) -> Extraction:
    """One `extract` call. Text first; a PDF whose text is garbled is read from its first page
    images instead (DOCX has no vision path). `force_vision` reads the images even when the
    text looks fine: the garble heuristic cannot see wrong reading order, so this is the escape
    hatch when a user says the parse is wrong."""
    use_vision = kind == "pdf" and (force_vision or parsed.garble.garbled)
    if use_vision:
        if not force_vision:
            log.warning(
                "resume: text layer unusable, falling back to page images: %s",
                "; ".join(parsed.garble.reasons),
            )
        try:
            images = await asyncio.to_thread(render_pages, data)
        except PdfReadError as error:
            raise ResumeReadError(str(error)) from error
        instructions, parts = vision_prompt(images)
        extracted = await structured(
            "extract", ExtractedResume, instructions=instructions, prompt=parts
        )
    else:
        instructions, prompt = text_prompt(parsed.text)
        extracted = await structured(
            "extract", ExtractedResume, instructions=instructions, prompt=prompt
        )
    return Extraction(extracted=extracted, used_vision=use_vision)


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    resume: Resume
    text: str  # the raw text pulled from the file, stored on the document row
    page_count: int
    used_vision: bool
    garble: GarbleReport


async def extract_resume(
    data: bytes,
    kind: DocumentType,
    *,
    source: ItemSource = "resume",
    force_vision: bool = False,
) -> ExtractionResult:
    """Reads and extracts a document; the resume gets fresh ids and `source` provenance."""
    parsed = await read_document(data, kind)
    extraction = await extract_from(parsed, data, kind, force_vision=force_vision)
    return ExtractionResult(
        resume=with_ids(extraction.extracted, source),
        text=parsed.text,
        page_count=parsed.page_count,
        used_vision=extraction.used_vision,
        garble=parsed.garble,
    )
