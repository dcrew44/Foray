"""Is this extracted text usable, or do we need to look at the page? (design §11.2; a port of
`src/lib/resume/parse/garble.ts`)

PDF text extraction fails in specific, measurable ways: a scanned page yields almost nothing, a
broken font map yields replacement characters, some exporters glue every word together, and
others letter-space everything into single characters. Each is cheap to detect, and detecting
them is what decides whether to spend a vision call.

What this deliberately does NOT claim to catch is *wrong reading order*: a two-column layout
extracted as interleaved columns is good text in a scrambled sequence. No character statistic
sees that; the extraction prompt handles it instead.

The thresholds and reason strings are today's. Counting is the Python way (design §11.2, §10.3):
lengths are code points, not UTF-16 units, and letters are `str.isalpha()` (the Unicode L*
categories, as `\\p{L}`). Whitespace is JavaScript's `\\s` class spelled out, and the numbers in
the reasons round as `Math.round` and `toFixed` do, so the strings match today's.
"""

from __future__ import annotations

import math
import re
from decimal import ROUND_HALF_UP, Decimal

from apps.runs.catalogue import GarbleReport

__all__ = ["JS_WHITESPACE", "GarbleReport", "detect_garble"]

# Below this, a page is effectively blank: almost always a scan.
MIN_CHARS_PER_PAGE = 180
# English prose runs ~0.15. Much lower means words are running together.
MIN_SPACE_RATIO = 0.08
# Letters as a share of non-space characters.
MIN_ALPHA_RATIO = 0.5
# Replacement and control characters mean a broken font or encoding map.
MAX_REPLACEMENT_RATIO = 0.01
# Letter-spaced extraction turns every glyph into its own "word".
MAX_SINGLE_CHAR_WORD_RATIO = 0.4

# JavaScript's `\s` (WhiteSpace and LineTerminator). Python's `\s` adds U+001C-U+001F and U+0085
# and leaves out U+FEFF.
JS_WHITESPACE = "\t\n\v\f\r \xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_SPACE = re.compile(f"[{JS_WHITESPACE}]")
_SPACE_RUN = re.compile(f"[{JS_WHITESPACE}]+")
# U+FFFD, plus control characters that should never survive extraction.
_BROKEN_ENCODING = re.compile("[\ufffd\x00-\x08\x0b\x0c\x0e-\x1f]")


def _js_round(value: float) -> int:
    """`Math.round`: halves round up."""
    return math.floor(value + 0.5)


def _to_fixed(value: float, digits: int) -> str:
    """`Number.prototype.toFixed` for the non-negative ratios here: the exact binary value,
    rounded half up."""
    return str(Decimal(value).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP))


def _number(value: float) -> int | float:
    """A metric as JavaScript serialises it: 900, not 900.0."""
    return int(value) if float(value).is_integer() else value


def detect_garble(text: str, page_count: int) -> GarbleReport:
    chars = len(text)
    pages = max(page_count, 1)

    spaces = len(_SPACE.findall(text))
    alpha = sum(1 for char in text if char.isalpha())
    replacement = len(_BROKEN_ENCODING.findall(text))

    words = [word for word in _SPACE_RUN.split(text) if word]
    single_char_words = sum(1 for word in words if len(word) == 1)

    non_space = max(chars - spaces, 1)

    chars_per_page = chars / pages
    space_ratio = 0 if chars == 0 else spaces / chars
    alpha_ratio = alpha / non_space
    replacement_ratio = 0 if chars == 0 else replacement / chars
    single_char_word_ratio = 0 if not words else single_char_words / len(words)

    reasons: list[str] = []
    if chars_per_page < MIN_CHARS_PER_PAGE:
        reasons.append(
            f"only {_js_round(chars_per_page)} characters per page — likely a scan or an "
            "image-only PDF"
        )
    if chars > 0 and space_ratio < MIN_SPACE_RATIO:
        reasons.append(
            f"word spacing lost ({_to_fixed(space_ratio, 3)} of characters are whitespace)"
        )
    if chars > 0 and alpha_ratio < MIN_ALPHA_RATIO:
        reasons.append(f"mostly non-letters ({_to_fixed(alpha_ratio, 2)} alphabetic)")
    if replacement_ratio > MAX_REPLACEMENT_RATIO:
        reasons.append("replacement or control characters — broken font encoding")
    if single_char_word_ratio > MAX_SINGLE_CHAR_WORD_RATIO:
        reasons.append(
            f"text is letter-spaced ({_to_fixed(single_char_word_ratio * 100, 0)}% "
            "single-character words)"
        )

    return GarbleReport(
        garbled=bool(reasons),
        reasons=reasons,
        metrics={
            "chars": chars,
            "charsPerPage": _number(chars_per_page),
            "spaceRatio": _number(space_ratio),
            "alphaRatio": _number(alpha_ratio),
            "replacementRatio": _number(replacement_ratio),
            "singleCharWordRatio": _number(single_char_word_ratio),
        },
    )
