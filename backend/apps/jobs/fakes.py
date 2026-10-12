"""The careers-page `extract` fake (design §8.9): the listing's links as postings.

Under `LLM_FAKE=1` every purpose resolves to a deterministic pydantic-ai `FunctionModel` (step
3B's `apps.llm.fake`), which answers structured calls with the smallest valid instance of the
output schema. For a careers listing that would be an empty listing, so every careers-page
company would look JavaScript-rendered. This override answers the listing extraction with the
page's own links instead: each `[label](url)` below the listing page becomes a posting titled
`label`, so the crawl, the posting fetches, ingest and the listing cache all run for $0.

`careers_listing_reply` is a `FunctionModel` function. It answers only a careers-listing request
(recognised by its instructions or by an output schema with `more_listing_urls`) and returns
None for anything else, so the resume `extract` override (step 3D) can answer those.
"""

from __future__ import annotations

import importlib
import re
from typing import Any

from ada_url import URL
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo

from apps.jobs.schemas import ExtractedPosting
from apps.jobs.sources.careers_page import LISTING_INSTRUCTIONS, MAX_POSTINGS, ExtractedListing

_PAGE_URL = re.compile(r"\ACareers page for .* at (\S+)\.\n")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")


def _below(url: str, listing_url: str) -> bool:
    """`url` is on the listing's host, under its path, and not the listing itself."""
    try:
        target, listing = URL(url), URL(listing_url)
    except ValueError:
        return False
    if target.host != listing.host:
        return False
    base = listing.pathname.rstrip("/") + "/"
    return target.pathname.startswith(base) and target.pathname.rstrip("/") != base.rstrip("/")


def fake_listing(prompt: str) -> ExtractedListing:
    """The postings a careers listing prompt links to, in page order, de-duplicated."""
    page = _PAGE_URL.match(prompt)
    listing_url = page.group(1) if page else ""
    postings: list[ExtractedPosting] = []
    seen: set[str] = set()
    for label, url in _LINK.findall(prompt):
        if url in seen or not _below(url, listing_url):
            continue
        seen.add(url)
        postings.append(ExtractedPosting(title=label.strip(), location=None, url=url, team=None))
        if len(postings) >= MAX_POSTINGS:
            break
    return ExtractedListing(postings=postings, more_listing_urls=[])


def _is_careers_listing(info: AgentInfo) -> bool:
    if info.instructions and LISTING_INSTRUCTIONS in info.instructions:
        return True
    for tool in info.output_tools:
        properties = tool.parameters_json_schema.get("properties") or {}
        if "more_listing_urls" in properties and "postings" in properties:
            return True
    return False


def _last_prompt(messages: list[ModelMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, ModelRequest):
            for part in reversed(message.parts):
                if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                    return part.content
    return ""


def careers_listing_reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse | None:
    if not _is_careers_listing(info) or not info.output_tools:
        return None
    listing = fake_listing(_last_prompt(messages))
    args: dict[str, Any] = listing.model_dump(mode="json")
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, args)])


def register() -> None:
    """Registers the override with step 3B's fake layer, when it is installed."""
    try:
        fake = importlib.import_module("apps.llm.fake")
    except ImportError:
        return
    register_override = getattr(fake, "register", None)
    if register_override is not None:
        register_override("extract", careers_listing_reply)


register()
