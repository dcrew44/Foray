"""HTML to readable text, minus the boilerplate nobody reads (design §10.3; `map/worker.md` §17.1).

A literal port of `clean.ts`, pinned by goldens. Two rules govern it:

1. `description_raw` is never touched. This produces a separate, disposable `description_text`;
   if the cleaning is wrong it can be regenerated.
2. Boilerplate is removed only by known patterns, never by summarising, and only from the tail
   (the last 30 %, invariant 5): every one of these patterns can legitimately appear in the body
   of a posting, so position is part of the evidence.

There is no HTML parser here on purpose: the TypeScript code is a chain of regex replacements,
and `description_text` feeds the careers-page `content_hash`, so the same text matters more than
better text (`research/documents-pdf.md` §4).
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field

from ada_url import join_url

from apps.jobs.sources.js import IA, JS_WS, S, js_trim

# Only the last 30% of a posting is eligible for boilerplate removal.
TAIL_FRACTION = 0.3

# Openers of blocks that are legal or administrative, not part of the job. Each is matched at the
# start of a paragraph in the tail. Deliberately conservative: a missed block costs a few tokens,
# a wrongly removed one costs a requirement.
BOILERPLATE_OPENERS: list[tuple[str, re.Pattern[str]]] = [
    (
        "equal opportunity",
        re.compile(
            rf"^(?:[\w{JS_WS}.,'&-]{{0,60}}\bis{S}+an{S}+)?equal{S}+(?:employment{S}+)?opportunity\b",
            IA,
        ),
    ),
    (
        "equal opportunity (we are)",
        re.compile(rf"^we{S}+are{S}+an{S}+equal{S}+(?:employment{S}+)?opportunity\b", IA),
    ),
    (
        "non-discrimination",
        re.compile(
            rf"^(?:all{S}+qualified{S}+applicants|we{S}+do{S}+not{S}+discriminate"
            rf"|qualified{S}+applicants{S}+will{S}+receive)\b",
            IA,
        ),
    ),
    (
        "E-Verify",
        re.compile(rf"^(?:e-verify\b|this{S}+employer{S}+participates{S}+in{S}+e-verify)", IA),
    ),
    (
        "pay transparency",
        re.compile(
            rf"^(?:pay{S}+transparency|the{S}+(?:pay|salary){S}+range{S}+(?:listed|shown|provided)"
            rf"|compensation{S}+range{S}+(?:listed|shown))\b",
            IA,
        ),
    ),
    (
        "accommodation",
        re.compile(
            rf"^(?:if{S}+you{S}+(?:need|require){S}+(?:an?{S}+)?(?:reasonable{S}+)?accommodation"
            rf"|we{S}+(?:are{S}+committed{S}+to{S}+)?provid(?:e|ing){S}+reasonable{S}+accommodation)",
            IA,
        ),
    ),
    (
        "fair chance / criminal history",
        re.compile(
            rf"^(?:pursuant{S}+to{S}+the{S}+(?:san{S}+francisco|los{S}+angeles)"
            rf"|qualified{S}+applicants{S}+with{S}+(?:arrest|criminal))",
            IA,
        ),
    ),
    (
        "agency notice",
        re.compile(
            rf"^(?:(?:no{S}+agencies|agencies{S}+only)"
            rf"|(?:we{S}+do{S}+not{S}+accept{S}+(?:unsolicited{S}+)?(?:resumes|agency)))",
            IA,
        ),
    ),
    (
        "privacy notice",
        re.compile(
            rf"^(?:(?:for{S}+)?(?:california|eu|uk){S}+(?:applicants|residents)"
            rf"|by{S}+(?:applying|submitting)[\w{JS_WS},]{{0,40}}\byou{S}+(?:consent|acknowledge))",
            IA,
        ),
    ),
]

BLOCK_TAGS = re.compile(
    r"</?(?:p|div|br|li|ul|ol|h[1-6]|tr|table|section|article|header|footer|blockquote)\b[^>]*>",
    IA,
)

# Entities that actually show up in ATS payloads.
ENTITIES = {
    "amp": "&",
    "lt": "<",
    "gt": ">",
    "quot": '"',
    "apos": "'",
    "nbsp": " ",  # a plain space, as the TypeScript table has it
    "ndash": "\u2013",
    "mdash": "\u2014",
    "rsquo": "\u2019",
    "lsquo": "\u2018",
    "ldquo": "\u201c",
    "rdquo": "\u201d",
    "hellip": "\u2026",
    "bull": "\u2022",
    "middot": "\u00b7",
}

_ENTITY = re.compile(r"&(#x?[0-9a-f]+|[a-z]+);", IA)
_LEADING_DECIMAL = re.compile(r"[0-9]+")  # what `Number.parseInt(s, 10)` consumes here
_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>[\s\S]*?</\1>", IA)
_LI_OPEN = re.compile(r"<li\b[^>]*>", IA)
_ANY_TAG = re.compile(r"<[^>]+>")
_CRLF = re.compile(r"\r\n?")
_SPACES = re.compile("[ \t\u00a0]+")
_NEWLINE_PADDING = re.compile(r" *\n *")
_BLANK_LINES = re.compile(r"\n{3,}")
_PARAGRAPH_BREAK = re.compile(r"\n{2,}")
_ESCAPED_TAG = re.compile(r"&lt;/?[a-z]", IA)
_LOOKS_LIKE_HTML = re.compile(r"</?[a-z][\s\S]*>", IA)
_LEADING_BULLET = re.compile(rf"^[{JS_WS}\u2022\-*]+")
_LINK = re.compile(rf"<a\b[^>]*?href{S}*={S}*[\"']([^\"']+)[\"'][^>]*>([\s\S]*?)</a>", IA)
_WHITESPACE_RUN = re.compile(f"{S}+")
_NAVIGATION = re.compile(r"^(?:#|javascript:|mailto:)", IA)


def _from_code_point(code: int) -> str:
    # `String.fromCodePoint` throws a RangeError above U+10FFFF, and so does this.
    return chr(code)


def decode_entities(text: str) -> str:
    """`&amp;`, `&#8212;`, `&#x2014;` and the named entities above; anything else as written.
    One pass, so nothing is decoded twice."""

    def replace(match: re.Match[str]) -> str:
        entity = match.group(1)
        if entity.startswith(("#x", "#X")):
            return _from_code_point(int(entity[2:], 16))
        if entity.startswith("#"):
            digits = _LEADING_DECIMAL.match(entity, 1)  # parseInt("12a", 10) is 12
            return _from_code_point(int(digits.group(0))) if digits else match.group(0)
        return ENTITIES.get(entity.lower(), match.group(0))

    return _ENTITY.sub(replace, text)


def html_to_text(html: str) -> str:
    """HTML to plain text, keeping paragraph and list structure."""
    text = _SCRIPT_STYLE.sub("", html)  # script and style content is never prose
    text = _LI_OPEN.sub("\n\u2022 ", text)  # list items become bullets
    text = BLOCK_TAGS.sub("\n", text)
    text = _ANY_TAG.sub("", text)
    text = decode_entities(text)
    text = _CRLF.sub("\n", text)
    text = _SPACES.sub(" ", text)
    text = _NEWLINE_PADDING.sub("\n", text)
    text = _BLANK_LINES.sub("\n\n", text)
    return js_trim(text)


@dataclass(slots=True)
class CleanResult:
    text: str
    # Which boilerplate blocks were removed, for auditing the patterns.
    removed: list[str] = field(default_factory=list)


def clean_description(raw: str | None) -> CleanResult:
    """`description_text` from a raw description, HTML or plain text."""
    if not raw:
        return CleanResult("", [])

    # Greenhouse entity-escapes its HTML, so tags arrive as `&lt;p&gt;`.
    unescaped = decode_entities(raw) if _ESCAPED_TAG.search(raw) else raw
    if _LOOKS_LIKE_HTML.search(unescaped):
        text = html_to_text(unescaped)
    else:
        text = js_trim(decode_entities(unescaped))

    paragraphs = _PARAGRAPH_BREAK.split(text)
    if len(paragraphs) < 2:
        return CleanResult(text, [])

    # Where the tail begins, in characters rather than paragraphs, so a posting that ends in many
    # short legal blocks is handled like one that ends in a single long one.
    cutoff = len(text) * (1 - TAIL_FRACTION)

    removed: list[str] = []
    kept: list[str] = []
    consumed = 0
    for paragraph in paragraphs:
        start = consumed
        consumed += len(paragraph) + 2
        if start < cutoff:
            kept.append(paragraph)
            continue
        opener = _LEADING_BULLET.sub("", paragraph, count=1)
        label = next((label for label, rule in BOILERPLATE_OPENERS if rule.search(opener)), None)
        if label is None:
            kept.append(paragraph)
        else:
            removed.append(label)

    return CleanResult(js_trim("\n\n".join(kept)), removed)


def html_to_text_with_links(html: str, base_url: str | None = None) -> str:
    """Like `html_to_text`, but keeps links as `[label](url)`.

    Plain-text conversion throws away every href, which is fine for a description and fatal for
    a careers listing: the extractor copies each posting's real link rather than inventing one.
    URLs are resolved against `base_url` with WHATWG rules (Ada, the parser Node uses).
    """

    def replace(match: re.Match[str]) -> str:
        href, inner = match.group(1), match.group(2)
        label = js_trim(_WHITESPACE_RUN.sub(" ", _ANY_TAG.sub(" ", inner)))
        if not label:
            return " "
        url = js_trim(href)
        if base_url:
            # A malformed href stays as written; the caller validates it again anyway.
            with contextlib.suppress(ValueError):
                url = join_url(base_url, url)
        # Anchors and javascript: links are navigation, not postings.
        if _NAVIGATION.search(url):
            return f" {label} "
        return f" [{label}]({url}) "

    return html_to_text(_LINK.sub(replace, html))
