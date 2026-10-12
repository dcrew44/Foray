"""The truthfulness check on a tailored resume (design §11.4; a verbatim port of
`src/lib/resume/verify.ts`).

A tailored resume that invents a skill, an employer or a number is the worst thing this product
can produce: the user sends it under their own name. So nothing the tailor writes is trusted.
Every line is checked against the base resume in two passes:

1. Deterministic rules, which cannot be talked round: items must be base items,
   names/titles/dates must be verbatim, skills must be base skills, and every number, link and
   technology-looking term in a rewritten line must already appear in the base item it sits
   under.
2. One cheap model call (`structured("verify", Verdicts, ...)`) over the rewritten lines that
   passed the rules, asking whether the original item actually supports each one: the check
   rules cannot do ("contributed to" becoming "led"). If the judge fails, every rewrite is
   reverted.

Anything flagged gets one deterministic revision: the line goes back to the original bullet it
most resembles, a changed field goes back to the base value, an unknown skill is dropped. Then
the rules run again (`finalize_verification`), and whatever still fails stays flagged for the
user to see.

Regex dialect: the TS patterns without the `u` flag are ASCII for `\\b`, `\\d` and case folding,
so they use `re` with `re.ASCII | re.IGNORECASE`; the ones with `\\p{L}`/`\\p{N}` use the
`regex` module, with `\\b` spelled as ASCII lookarounds. JavaScript's `\\s` is spelled out
everywhere (`text.JS_SPACE`). Lengths and slices are Python-native (code points).
"""

from __future__ import annotations

import logging
import math
import re
import unicodedata
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import regex
from pydantic import BaseModel

from apps.core.jsonio import js_num
from apps.llm.structured import structured
from apps.resumes.schema import (
    Resume,
    Revision,
    TailorChange,
    TailorSection,
    UnsupportedClaim,
    Verification,
    fact_ids,
)
from apps.resumes.text import JS_SPACE, js_trim, resume_to_text

__all__ = [
    "BasicsTarget",
    "Claim",
    "FieldTarget",
    "Finding",
    "ItemTarget",
    "Judge",
    "SkillTarget",
    "Target",
    "TextTarget",
    "Verdict",
    "Verdicts",
    "VerifyResult",
    "build_judge_prompt",
    "check_rules",
    "collect_claims",
    "extract_links",
    "extract_metrics",
    "extract_terms",
    "finalize_verification",
    "model_judge",
    "normalize_text",
    "reconcile_changes",
    "revise_tailored",
    "similarity",
    "split_sentences",
    "verify_tailored",
]

log = logging.getLogger(__name__)

ItemSection = Literal["work", "education", "projects"]
ClaimField = Literal["highlight", "summary", "description", "sentence"]

_SPACE_CLASS = re.escape(JS_SPACE)
_WS = f"[{_SPACE_CLASS}]"
_ASCII_I = re.ASCII | re.IGNORECASE
# `\b` of a JS pattern, as ASCII lookarounds, for the `regex` patterns (which need Unicode
# property classes, so they cannot take the ASCII flag).
_WORD_END = r"(?![A-Za-z0-9_])"

# --- Where a problem is ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BasicsTarget:
    field: Literal["name", "email", "phone", "location", "headline", "links"]


@dataclass(frozen=True, slots=True)
class ItemTarget:
    section: ItemSection
    item_id: str


@dataclass(frozen=True, slots=True)
class FieldTarget:
    section: ItemSection
    item_id: str
    field: str  # the attribute name: "start_date", "position", ...


@dataclass(frozen=True, slots=True)
class SkillTarget:
    index: int


@dataclass(frozen=True, slots=True)
class TextTarget:
    key: str


Target = BasicsTarget | ItemTarget | FieldTarget | SkillTarget | TextTarget


@dataclass(frozen=True, slots=True)
class Finding:
    """A problem found in a tailored resume: the stored `UnsupportedClaim` plus where it is,
    precisely enough for the revision to fix it."""

    section: str
    item_id: str | None
    text: str
    reason: str
    source: Literal["rule", "model"]
    target: Target

    def public(self) -> UnsupportedClaim:
        return UnsupportedClaim(
            section=self.section,
            item_id=self.item_id,
            text=self.text,
            reason=self.reason,
            source=self.source,
        )


# --- Claims ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Claim:
    """One line of tailored text that asserts something about the candidate."""

    key: str
    section: TailorSection
    item_id: str | None
    field: ClaimField
    index: int
    text: str
    verbatim: bool  # identical to base text in the same place: true by construction


_QUOTES_AND_DASHES = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
    }
)
_WS_RUN = re.compile(f"{_WS}+")
_TRAILING_PUNCTUATION = re.compile(r"[.;,]+\Z")


def normalize_text(value: str) -> str:
    """Case, whitespace, quote and dash differences do not make a line new."""
    value = unicodedata.normalize("NFKC", value).lower().translate(_QUOTES_AND_DASHES)
    value = js_trim(_WS_RUN.sub(" ", value))
    return _TRAILING_PUNCTUATION.sub("", value)


_SENTENCE_BREAK = re.compile(f'(?<=[a-z0-9%)"\'’”][.!?]){_WS}+(?=[A-Z"“(])')


def split_sentences(text: str) -> list[str]:
    """Sentences, split conservatively: only after a lowercase word, digit or closing bracket,
    so "B.S. in", "U.S. Army" and "e.g" do not split. A missed split merges two sentences into
    one claim, which is safe; a wrong split could leave half a sentence behind when the other
    half is dropped."""
    return [js_trim(part) for part in _SENTENCE_BREAK.split(text) if js_trim(part)]


def _filled(value: str | None) -> bool:
    return bool(value and js_trim(value))


def collect_claims(base: Resume, tailored: Resume) -> list[Claim]:
    claims: list[Claim] = []

    def same(a: str | None, b: str) -> bool:
        return a is not None and _filled(a) and normalize_text(a) == normalize_text(b)

    if tailored.summary is not None and _filled(tailored.summary):
        base_sentences = {normalize_text(s) for s in split_sentences(base.summary or "")}
        whole = same(base.summary, tailored.summary)
        for index, text in enumerate(split_sentences(tailored.summary)):
            claims.append(
                Claim(
                    key=f"summary:sentence:{index}",
                    section="summary",
                    item_id=None,
                    field="sentence",
                    index=index,
                    text=text,
                    verbatim=whole or normalize_text(text) in base_sentences,
                )
            )

    def bullets_of(
        section: ItemSection, item_id: str, highlights: list[str], original: list[str] | None
    ) -> None:
        known = {normalize_text(line) for line in original or []}
        for index, text in enumerate(highlights):
            claims.append(
                Claim(
                    key=f"{section}:{item_id}:highlight:{index}",
                    section=section,
                    item_id=item_id,
                    field="highlight",
                    index=index,
                    text=text,
                    verbatim=normalize_text(text) in known,
                )
            )

    base_work = {item.id: item for item in base.work}
    for item in tailored.work:
        original = base_work.get(item.id)
        if item.summary is not None and _filled(item.summary):
            claims.append(
                Claim(
                    key=f"work:{item.id}:summary:0",
                    section="work",
                    item_id=item.id,
                    field="summary",
                    index=0,
                    text=item.summary,
                    verbatim=same(original.summary if original else None, item.summary),
                )
            )
        bullets_of("work", item.id, item.highlights, original.highlights if original else None)

    base_projects = {item.id: item for item in base.projects}
    for item in tailored.projects:
        original = base_projects.get(item.id)
        if item.description is not None and _filled(item.description):
            claims.append(
                Claim(
                    key=f"projects:{item.id}:description:0",
                    section="projects",
                    item_id=item.id,
                    field="description",
                    index=0,
                    text=item.description,
                    verbatim=same(original.description if original else None, item.description),
                )
            )
        bullets_of("projects", item.id, item.highlights, original.highlights if original else None)

    base_education = {item.id: item for item in base.education}
    for item in tailored.education:
        original = base_education.get(item.id)
        bullets_of("education", item.id, item.highlights, original.highlights if original else None)

    return claims


# --- Numbers, links and terms ---------------------------------------------------------------------

_URL = re.compile(
    rf"\bhttps?://[^{_SPACE_CLASS})<>\"']+|\b(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\."
    r"(?:com|org|net|io|dev|ai|co|app|edu|gov|me|xyz|tech|so|sh|gg|us|uk|ca|de|fr|in|info|biz"
    r"|tv|cloud|site|page|blog|design|codes)\b"
    rf"(?:/[^{_SPACE_CLASS})<>\"',;]*)?",
    _ASCII_I,
)
_EMAIL = re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", _ASCII_I)


def _strip_links(text: str) -> str:
    return _URL.sub(" ", _EMAIL.sub(" ", text))


_LINK_PREFIXES = (re.compile(r"^https?://"), re.compile(r"^www\."))
_LINK_TAIL = re.compile(r"[/.,;:)]+\Z")


def _normalize_link(value: str) -> str:
    value = value.lower()
    for prefix in _LINK_PREFIXES:
        value = prefix.sub("", value, count=1)
    return _LINK_TAIL.sub("", value)


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def extract_links(text: str) -> list[str]:
    """URLs and email addresses mentioned in a line, normalized for comparison."""
    emails = [match.group(0) for match in _EMAIL.finditer(text)]
    urls = [match.group(0) for match in _URL.finditer(_EMAIL.sub(" ", text))]
    return _unique(_normalize_link(link) for link in [*emails, *urls])


_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_UNITS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
}
# Number words that state a quantity. "one" is left out: it is far more often "one of" than a
# metric, and flagging it would revert harmless rewrites.
_NUMBER_WORDS = {
    **{word: value for word, value in _UNITS.items() if word != "one"},
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    **_TENS,
    "hundred": 100,
    "dozen": 12,
}
_MULTIPLIER_WORDS = {
    "twice": 2,
    "doubled": 2,
    "doubling": 2,
    "twofold": 2,
    "tripled": 3,
    "tripling": 3,
    "threefold": 3,
    "quadrupled": 4,
    "fourfold": 4,
    "fivefold": 5,
    "tenfold": 10,
}


@dataclass(frozen=True, slots=True)
class Metric:
    """A number as written, and every value it could reasonably stand for."""

    raw: str
    keys: list[str]


def _fmt(value: float) -> str:
    """`String(Number(value.toPrecision(12)))`."""
    return js_num(float(f"{value:.12g}"))


_NOT_LN = r"(?![\p{L}\p{N}])"
# What follows a number decides what it means: "40%" and "40-million-row" share a digit and
# nothing else. Each suffix maps to comparison keys; an ambiguous one ("26m": minutes or
# million) yields every reading, and a number passes if any reading appears in the base.
_SUFFIXES: list[tuple[Any, Callable[[float], list[str]]]] = [
    (
        re.compile(f"^{_WS}?(?:%|percent\\b|per{_WS}?cent\\b|pct\\b)", _ASCII_I),
        lambda v: [f"pct:{_fmt(v)}"],
    ),
    (
        regex.compile(f"^{_WS}?-?{_WS}?(?:x|×){_NOT_LN}", regex.IGNORECASE),
        lambda v: [f"x:{_fmt(v)}"],
    ),
    (re.compile(f"^{_WS}?-?{_WS}?fold\\b", _ASCII_I), lambda v: [f"x:{_fmt(v)}"]),
    (re.compile(f"^{_WS}?times\\b", _ASCII_I), lambda v: [f"x:{_fmt(v)}", f"n:{_fmt(v)}"]),
    (
        regex.compile(f"^{_WS}?-?{_WS}?(?:thousand{_WORD_END}|k{_NOT_LN})", regex.IGNORECASE),
        lambda v: [f"n:{_fmt(v * 1e3)}"],
    ),
    (
        re.compile(f"^{_WS}?-?{_WS}?(?:million\\b|mil\\b|mn\\b)", _ASCII_I),
        lambda v: [f"n:{_fmt(v * 1e6)}"],
    ),
    (regex.compile(f"^{_WS}?-?{_WS}?(?:MM|M){_NOT_LN}"), lambda v: [f"n:{_fmt(v * 1e6)}"]),
    (
        regex.compile(f"^{_WS}?-?{_WS}?(?:mm|m){_NOT_LN}"),
        lambda v: [f"n:{_fmt(v * 1e6)}", f"n:{_fmt(v)}"],
    ),
    (
        re.compile(f"^{_WS}?-?{_WS}?(?:billion\\b|bn\\b)", _ASCII_I),
        lambda v: [f"n:{_fmt(v * 1e9)}"],
    ),
    (regex.compile(f"^{_WS}?-?{_WS}?B{_NOT_LN}"), lambda v: [f"n:{_fmt(v * 1e9)}"]),
]

# A number glued to a letter ("S3", "EC2", "Q3", "v2") is a name, not a quantity, and is left to
# the term check.
_DIGITS = regex.compile(
    r"(?<![\p{L}\p{N}_.])([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)"
)
_WORDS = re.compile(
    rf"\b(?:({'|'.join(_TENS)})[-{_SPACE_CLASS}]({'|'.join(_UNITS)})"
    rf"|({'|'.join(_NUMBER_WORDS)})|({'|'.join(_MULTIPLIER_WORDS)}))\b",
    _ASCII_I,
)


def extract_metrics(text: str) -> list[Metric]:
    clean = _strip_links(text)
    metrics: list[Metric] = []

    for match in _DIGITS.finditer(clean):
        value = float(match.group(1).replace(",", ""))
        if not math.isfinite(value):
            continue
        rest = clean[match.end() :]
        for pattern, keys in _SUFFIXES:
            suffix = pattern.match(rest)
            if suffix:
                metrics.append(Metric(raw=match.group(0) + suffix.group(0), keys=keys(value)))
                break
        else:
            metrics.append(Metric(raw=match.group(0), keys=[f"n:{_fmt(value)}"]))

    # Words: "twenty-five", "six", "tripled".
    for match in _WORDS.finditer(clean):
        tens, units, word, multiplier = match.groups()
        if tens and units:
            value = _TENS[tens.lower()] + _UNITS[units.lower()]
            metrics.append(Metric(raw=match.group(0), keys=[f"n:{value}"]))
        elif word:
            metrics.append(Metric(raw=match.group(0), keys=[f"n:{_NUMBER_WORDS[word.lower()]}"]))
        elif multiplier:
            metrics.append(
                Metric(raw=match.group(0), keys=[f"x:{_MULTIPLIER_WORDS[multiplier.lower()]}"])
            )

    return metrics


def _metric_keys(text: str) -> set[str]:
    return {key for metric in extract_metrics(text) for key in metric.keys}


_TOKEN = regex.compile(r"[\p{L}\p{N}][\p{L}\p{N}.+#'’/-]*")
_POSSESSIVE = re.compile(r"['’]s\Z", _ASCII_I)
_TOKEN_TAIL = re.compile(r"[.\-/'’]+\Z")
_SPLIT_PARTS = re.compile(r"[/-]")
_EDGE_DOTS = re.compile(r"^\.+|\.+\Z")
_LETTER = regex.compile(r"\p{L}")
_UPPER = regex.compile(r"\p{Lu}")
_SYMBOLIC = re.compile(r"[0-9.+#]")


def _is_sentence_start(text: str, index: int) -> bool:
    before = text[:index].rstrip(JS_SPACE)
    return not before or before[-1] in ".!?:;•"


def extract_terms(text: str) -> list[str]:
    """Words that name something: a technology, product, company or credential.

    Mixed case ("TypeScript", "AWS", "gRPC"), inner digits or symbols ("Node.js", "C++", "S3"),
    or a capital letter in the middle of a sentence ("with Kubernetes"). A capitalised first
    word is usually a verb ("Built"), so it only counts when it is mixed-case or symbolic."""
    clean = _strip_links(text)
    terms: list[str] = []
    for match in _TOKEN.finditer(clean):
        token = _TOKEN_TAIL.sub("", _POSSESSIVE.sub("", match.group(0)))
        initial = _is_sentence_start(clean, match.start())
        for index, part in enumerate(_SPLIT_PARTS.split(token)):
            word = _EDGE_DOTS.sub("", part)
            if len(word) < 2 or word[0] in "0123456789" or not _LETTER.search(word):
                continue
            rest = word[1:]
            mixed = bool(_UPPER.search(rest))
            symbolic = bool(_SYMBOLIC.search(rest))
            capital = bool(_UPPER.match(word)) and not (initial and index == 0)
            if mixed or symbolic or capital:
                terms.append(word)
    return _unique(terms)


# Equivalent spellings, so a rewrite may use the posting's name for a base term.
_ALIASES: dict[str, list[str]] = {
    "postgres": ["postgresql"],
    "postgresql": ["postgres"],
    "k8s": ["kubernetes"],
    "kubernetes": ["k8s"],
    "js": ["javascript"],
    "ts": ["typescript"],
    "golang": ["go"],
    "node": ["node.js", "nodejs"],
    "nodejs": ["node.js", "node"],
    "node.js": ["node", "nodejs"],
    "reactjs": ["react"],
    "react.js": ["react"],
    "react": ["react.js", "reactjs"],
    "next": ["next.js"],
    "next.js": ["next"],
    "vue.js": ["vue"],
    "vue": ["vue.js"],
}
_LETTER_OR_NUMBER = regex.compile(r"[\p{L}\p{N}]")


def _contains_word(haystack: str, needle: str) -> bool:
    def boundary(char: str | None) -> bool:
        return char is None or not _LETTER_OR_NUMBER.match(char)

    start = 0
    while True:
        at = haystack.find(needle, start)
        if at < 0:
            return False
        before = haystack[at - 1] if at > 0 else None
        end = at + len(needle)
        after = haystack[end] if end < len(haystack) else None
        if boundary(before) and boundary(after):
            return True
        start = at + 1


def _term_in(term: str, scope_lower: str) -> bool:
    lower = term.lower()
    variants = [lower, *_ALIASES.get(lower, [])]
    # Plurals and possessives of a base term: "APIs" for "API".
    if len(lower) > 3 and lower.endswith("s"):
        variants.append(lower[:-1])
    if len(lower) > 4 and lower.endswith("es"):
        variants.append(lower[:-2])
    return any(_contains_word(scope_lower, variant) for variant in variants)


# --- Scopes ---------------------------------------------------------------------------------------


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    if isinstance(value, dict):
        return [
            text
            for key, inner in value.items()
            if key not in ("id", "source")
            for text in _strings(inner)
        ]
    return []


@dataclass(frozen=True, slots=True)
class _Scope:
    text: str
    lower: str
    metrics: set[str]


def _scope(text: str) -> _Scope:
    return _Scope(text=text, lower=text.lower(), metrics=_metric_keys(text))


def _scopes(base: Resume) -> tuple[dict[str, _Scope], _Scope]:
    """What a line may draw on. A bullet may only use its own item: moving a number from one
    job to another is fabrication too. The summary may draw on the whole resume."""
    items = {
        item.id: _scope("\n".join(_strings(item.dump())))
        for item in [*base.work, *base.education, *base.projects]
    }
    return items, _scope("\n".join(_strings(base.dump())))


# --- Deterministic rules --------------------------------------------------------------------------

_STRICT_FIELDS: dict[ItemSection, list[str]] = {
    "work": ["company", "position", "start_date", "end_date"],
    "education": ["institution", "area", "study_type", "start_date", "end_date"],
    "projects": ["name", "url", "start_date", "end_date"],
}
# Fields a tailored resume may leave out, but never change.
_OMITTABLE_FIELDS: dict[ItemSection, list[str]] = {
    "work": ["location"],
    "education": ["score"],
    "projects": [],
}
_FIELD_LABELS = {
    "company": "company name",
    "position": "job title",
    "start_date": "start date",
    "end_date": "end date",
    "location": "location",
    "institution": "school",
    "area": "field of study",
    "study_type": "degree",
    "score": "grade",
    "name": "project name",
    "url": "project link",
}
_SECTION_NOUN: dict[str, str] = {
    "work": "role",
    "education": "education entry",
    "projects": "project",
}
_BASICS_FIELDS: tuple[Literal["name", "email", "phone", "location", "headline"], ...] = (
    "name",
    "email",
    "phone",
    "location",
    "headline",
)


def _value(raw: Any) -> str | None:
    return js_trim(raw) if isinstance(raw, str) and js_trim(raw) else None


def _list_phrase(items: list[str]) -> str:
    quoted = items[:4]
    if len(quoted) == 1:
        return quoted[0]
    return f"{', '.join(quoted[:-1])} and {quoted[-1]}"


_WWW = re.compile(r"\bwww\.", re.ASCII)
_SCHEME = re.compile(r"https?://")


def _normalize_link_corpus(text: str) -> str:
    return _WWW.sub("", _SCHEME.sub("", text.lower()))


def _skill_name(name: str) -> str:
    return js_trim(_WS_RUN.sub(" ", unicodedata.normalize("NFKC", name).lower()))


def check_rules(base: Resume, tailored: Resume) -> list[Finding]:
    """Every check that needs no model. Pure, so it can run before and after the revision and
    be tested exhaustively."""
    findings: list[Finding] = []

    # Basics are copied from the base; anything else is a defect upstream.
    for field in _BASICS_FIELDS:
        now = _value(getattr(tailored.basics, field))
        if now != _value(getattr(base.basics, field)):
            findings.append(
                Finding(
                    section="basics",
                    item_id=None,
                    text=now or "",
                    reason=f"Your {field} must match your resume exactly.",
                    source="rule",
                    target=BasicsTarget(field),
                )
            )

    def link_key(resume: Resume) -> list[tuple[str, str]]:
        return [(js_trim(link.label), js_trim(link.url)) for link in resume.basics.links]

    if link_key(tailored) != link_key(base):
        findings.append(
            Finding(
                section="basics",
                item_id=None,
                text=", ".join(link.url for link in tailored.basics.links),
                reason="Your links must match your resume exactly.",
                source="rule",
                target=BasicsTarget("links"),
            )
        )

    # Items: each must be a base item, with its facts unchanged.
    sections: list[tuple[ItemSection, list[Any], list[Any]]] = [
        ("work", base.work, tailored.work),
        ("education", base.education, tailored.education),
        ("projects", base.projects, tailored.projects),
    ]
    for section, base_items, items in sections:
        by_id = {item.id: item for item in base_items}
        for item in items:
            original = by_id.get(item.id)
            if original is None:
                title = (
                    _value(getattr(item, "position", None))
                    or _value(getattr(item, "institution", None))
                    or _value(getattr(item, "name", None))
                )
                findings.append(
                    Finding(
                        section=section,
                        item_id=item.id,
                        text=" — ".join(
                            part for part in (title, _value(getattr(item, "company", None))) if part
                        ),
                        reason=f"Not a {_SECTION_NOUN[section]} on your resume.",
                        source="rule",
                        target=ItemTarget(section, item.id),
                    )
                )
                continue

            for field in [*_STRICT_FIELDS[section], *_OMITTABLE_FIELDS[section]]:
                now = _value(getattr(item, field))
                was = _value(getattr(original, field))
                if now == was:
                    continue
                if now is None and field in _OMITTABLE_FIELDS[section]:
                    continue
                label = _FIELD_LABELS[field]
                if now is None:
                    reason = f'Removed the {label} "{was}".'
                elif was is None:
                    reason = f'Added a {label} ("{now}") that is not on your resume.'
                else:
                    reason = f'Changed the {label} from "{was}" to "{now}".'
                findings.append(
                    Finding(
                        section=section,
                        item_id=item.id,
                        text=now or "",
                        reason=reason,
                        source="rule",
                        target=FieldTarget(section, item.id, field),
                    )
                )

    # Skills: a reordered subset of the base skills, nothing more.
    known = {_skill_name(skill.name) for skill in base.skills}
    for index, skill in enumerate(tailored.skills):
        if _skill_name(skill.name) in known:
            continue
        findings.append(
            Finding(
                section="skills",
                item_id=None,
                text=skill.name,
                reason=f"{js_trim(skill.name)} is not a skill on your resume.",
                source="rule",
                target=SkillTarget(index),
            )
        )

    # Rewritten lines: every number, link and named thing must already be in the base item
    # (or, for the summary, the base resume).
    item_scopes, resume_scope = _scopes(base)
    resume_links = _normalize_link_corpus(resume_scope.text)

    for claim in collect_claims(base, tailored):
        if claim.verbatim:
            continue
        within = item_scopes.get(claim.item_id) if claim.item_id else resume_scope
        if within is None:
            continue  # an unknown item is reported above

        numbers = [
            js_trim(metric.raw)
            for metric in extract_metrics(claim.text)
            if not any(key in within.metrics for key in metric.keys)
        ]
        links = [link for link in extract_links(claim.text) if link not in resume_links]
        terms = [term for term in extract_terms(claim.text) if not _term_in(term, within.lower)]

        problems = _unique([*numbers, *terms])
        where = (
            f"this {_SECTION_NOUN[claim.section]} on your resume"
            if claim.item_id
            else "your resume"
        )
        reasons: list[str] = []
        if problems:
            verb = "isn't" if len(problems) == 1 else "aren't"
            reasons.append(f"Mentions {_list_phrase(problems)}, which {verb} in {where}.")
        if links:
            verb = "isn't" if len(links) == 1 else "aren't"
            reasons.append(f"Links to {_list_phrase(links)}, which {verb} on your resume.")
        if reasons:
            findings.append(
                Finding(
                    section=claim.section,
                    item_id=claim.item_id,
                    text=claim.text,
                    reason=" ".join(reasons),
                    source="rule",
                    target=TextTarget(claim.key),
                )
            )

    return findings


# --- The model check ------------------------------------------------------------------------------

JUDGE_INSTRUCTIONS = """You audit a tailored resume line by line against the candidate's original resume. The original is the only source of truth about the candidate.

For each claim, decide whether the ORIGINAL ITEM it belongs to supports it. For summary claims, the whole original resume is the evidence.

Supported: every fact in the claim is stated in, or directly and unambiguously implied by, the evidence. Rewording, shortening, reordering, merging facts from the same item, and an equivalent term ("Postgres" for "PostgreSQL", "built" for "implemented") are all fine.

Unsupported, if the claim has any of these:
- a tool, technology, language, framework, method, domain or skill the evidence does not mention for this item;
- a number, metric, percentage, team size, duration, scale or date the evidence does not state — including numbers computed from the evidence ("840 ms to 190 ms" is not "77% faster");
- an outcome, impact, customer or scope the evidence does not state;
- more ownership or seniority than the evidence shows ("contributed to" is not "led", "helped" is not "owned", "worked with" is not "managed");
- a fact that comes from a different item of the resume.

Be strict: when unsure, it is unsupported. The candidate will send this resume to employers under their own name.

reason: for an unsupported claim, one short sentence naming exactly what is not supported, e.g. "The original does not mention Kubernetes." For a supported claim, null.
Return one verdict for every claim id."""  # noqa: E501 (prompt text, pinned by the §8.10 goldens)


class VerdictItem(BaseModel):
    claim_id: str
    supported: bool
    reason: str | None


class Verdicts(BaseModel):
    verdicts: list[VerdictItem]


@dataclass(frozen=True, slots=True)
class Verdict:
    supported: bool
    reason: str | None


# Judges claims against the base resume. Injected, so the pipeline can be tested without a
# model; the default makes one `verify` call per resume.
type Judge = Callable[[Resume, list[Claim], list[TailorChange]], Awaitable[dict[str, Verdict]]]


def _join_present(parts: Iterable[str | None], separator: str) -> str:
    return separator.join(part for part in parts if part)


def _item_evidence(base: Resume, section: str, item_id: str | None) -> str:
    if not item_id:
        return "(the whole original resume, above)"
    for work in base.work:
        if work.id == item_id:
            return _join_present(
                [
                    f"{work.position} — {work.company}"
                    + (f" ({work.location})" if work.location else ""),
                    _join_present([work.start_date, work.end_date], " – "),
                    work.summary,
                    *(f"• {line}" for line in work.highlights),
                ],
                "\n",
            )
    for project in base.projects:
        if project.id == item_id:
            return _join_present(
                [
                    project.name + (f" — {project.url}" if project.url else ""),
                    project.description,
                    *(f"• {line}" for line in project.highlights),
                ],
                "\n",
            )
    for education in base.education:
        if education.id == item_id:
            return _join_present(
                [
                    _join_present(
                        [education.study_type, education.area, education.institution], ", "
                    ),
                    _join_present([education.start_date, education.end_date], " – "),
                    education.score,
                    *(f"• {line}" for line in education.highlights),
                ],
                "\n",
            )
    return f"(no {section} item with this id)"


def build_judge_prompt(
    base: Resume, claims: list[Claim], changes: list[TailorChange]
) -> tuple[str, str, dict[str, Claim]]:
    """`(instructions, prompt, claims by id)` for the judge: the original resume in the
    instructions (the stable, cacheable half), the claims grouped by item in the prompt."""
    ids = {f"c{index + 1}": claim for index, claim in enumerate(claims)}

    # The tailor's own change log pairs a rewrite with the bullet it came from, which makes the
    # comparison far easier to get right.
    origins: dict[str, str] = {}
    for change in changes:
        if change.after and change.before:
            origins[normalize_text(change.after)] = change.before

    groups: dict[str, list[tuple[str, Claim]]] = {}
    for claim_id, claim in ids.items():
        groups.setdefault(f"{claim.section}:{claim.item_id or ''}", []).append((claim_id, claim))

    blocks: list[str] = []
    for group in groups.values():
        first = group[0][1]
        claims_xml = "\n".join(
            f'<claim id="{claim_id}">\n{claim.text}'
            + (
                f"\n<rewritten_from>{origin}</rewritten_from>"
                if (origin := origins.get(normalize_text(claim.text)))
                else ""
            )
            + "\n</claim>"
            for claim_id, claim in group
        )
        item_attribute = f' id="{first.item_id}"' if first.item_id else ""
        blocks.append(
            f'<item section="{first.section}"{item_attribute}>\n<original>\n'
            f"{_item_evidence(base, first.section, first.item_id)}\n</original>\n"
            f"{claims_xml}\n</item>"
        )

    instructions = (
        f"{JUDGE_INSTRUCTIONS}\n\n<original_resume>\n{resume_to_text(base)}\n</original_resume>"
    )
    prompt = "Check these claims.\n\n" + "\n\n".join(blocks)
    return instructions, prompt, ids


async def model_judge(
    base: Resume, claims: list[Claim], changes: list[TailorChange]
) -> dict[str, Verdict]:
    instructions, prompt, ids = build_judge_prompt(base, claims, changes)
    result = await structured("verify", Verdicts, instructions=instructions, prompt=prompt)
    verdicts: dict[str, Verdict] = {}
    for verdict in result.verdicts:
        claim = ids.get(js_trim(verdict.claim_id))
        if claim is not None:
            verdicts[claim.key] = Verdict(supported=verdict.supported, reason=verdict.reason)
    return verdicts


@dataclass(frozen=True, slots=True)
class VerifyResult:
    findings: list[Finding]  # every problem found, rules first, one per location
    checked: int
    model_checked: int
    model_error: str | None


def _text_finding(claim: Claim, reason: str, source: Literal["rule", "model"]) -> Finding:
    return Finding(
        section=claim.section,
        item_id=claim.item_id,
        text=claim.text,
        reason=reason,
        source=source,
        target=TextTarget(claim.key),
    )


async def verify_tailored(
    base: Resume,
    tailored: Resume,
    changes: list[TailorChange],
    *,
    judge: Judge | None = None,
) -> VerifyResult:
    """Both passes over a tailored resume. Rules first; the model only judges the rewritten
    lines that passed them, so it is never asked about a line that will be reverted anyway."""
    findings = check_rules(base, tailored)
    claims = collect_claims(base, tailored)
    flagged = {f.target.key for f in findings if isinstance(f.target, TextTarget)}
    to_judge = [claim for claim in claims if not claim.verbatim and claim.key not in flagged]

    model_error: str | None = None
    if to_judge:
        try:
            verdicts = await (judge or model_judge)(base, to_judge, changes)
        except Exception as error:
            # An unchecked rewrite is not a verified one. Rather than save lines no one has
            # checked, every rewrite goes back to the original wording.
            log.warning("verify: the judge failed: %r", error)
            model_error = str(error) or type(error).__name__
            findings.extend(
                _text_finding(
                    claim, "Could not be checked, so your original wording was kept.", "model"
                )
                for claim in to_judge
            )
        else:
            for claim in to_judge:
                verdict = verdicts.get(claim.key)
                if verdict is not None and verdict.supported:
                    continue
                if verdict is None:
                    reason = "The check returned no verdict for this line, so it was not trusted."
                else:
                    reason = js_trim(verdict.reason or "") or "Not supported by your resume."
                findings.append(_text_finding(claim, reason, "model"))

    return VerifyResult(
        findings=findings,
        checked=len(claims) + len(tailored.skills),
        model_checked=len(to_judge),
        model_error=model_error,
    )


# --- The revision ---------------------------------------------------------------------------------

_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "the",
        "of",
        "for",
        "to",
        "in",
        "on",
        "at",
        "by",
        "with",
        "from",
        "into",
        "over",
        "under",
        "as",
        "or",
        "that",
        "which",
        "this",
        "than",
        "their",
        "its",
        "our",
        "your",
        "was",
        "were",
        "is",
        "are",
        "be",
        "been",
        "used",
        "using",
    ]
)
_STEM_SUFFIXES = ("ments", "ment", "ions", "ion", "ing", "ies", "ed", "es", "ly", "s")
_NON_WORD = regex.compile(r"[^\p{L}\p{N}.%]+")
_TRAILING_DOTS = re.compile(r"\.+\Z")


def _stem(word: str) -> str:
    """A crude suffix stemmer: enough that "partitioned" and "partitioning" count as the same
    word when matching a rewrite back to the bullet it came from."""
    for suffix in _STEM_SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _content_words(text: str) -> set[str]:
    words = (_TRAILING_DOTS.sub("", word) for word in _NON_WORD.split(normalize_text(text)))
    return {_stem(word) for word in words if len(word) > 2 and word not in _STOPWORDS}


def similarity(a: str, b: str) -> float:
    """Dice similarity over content words: which original a rewrite came from."""
    left = _content_words(a)
    right = _content_words(b)
    if not left or not right:
        return 0
    shared = sum(1 for word in left if word in right)
    return (2 * shared) / (len(left) + len(right))


# Below this, a flagged line resembles no original bullet and is dropped.
MIN_RESEMBLANCE = 0.15


def _revision(
    finding: Finding, action: Literal["replaced", "dropped", "reverted"], replacement: str | None
) -> Revision:
    return Revision(
        section=finding.section,
        item_id=finding.item_id,
        text=finding.text,
        reason=finding.reason,
        source=finding.source,
        action=action,
        replacement=replacement,
    )


def revise_tailored(
    base: Resume, tailored: Resume, findings: list[Finding]
) -> tuple[Resume, list[Revision]]:
    """The one automatic revision. Deterministic, and it only ever moves the resume towards the
    base: a flagged line becomes the original bullet it most resembles (or is dropped), a
    changed field goes back to its base value, an unknown item or skill is removed. It never
    writes new text."""
    resume = tailored.model_copy(deep=True)
    revisions: list[Revision] = []

    # Basics.
    for finding in findings:
        target = finding.target
        if not isinstance(target, BasicsTarget):
            continue
        if target.field == "links":
            resume.basics.links = [link.model_copy(deep=True) for link in base.basics.links]
            replacement = ", ".join(link.url for link in base.basics.links)
            revisions.append(_revision(finding, "reverted", replacement))
        else:
            setattr(resume.basics, target.field, getattr(base.basics, target.field))
            revisions.append(
                _revision(finding, "reverted", _value(getattr(base.basics, target.field)))
            )

    # Unknown items.
    unknown = {
        (f.target.section, f.target.item_id) for f in findings if isinstance(f.target, ItemTarget)
    }
    if unknown:
        resume.work = [item for item in resume.work if ("work", item.id) not in unknown]
        resume.education = [
            item for item in resume.education if ("education", item.id) not in unknown
        ]
        resume.projects = [item for item in resume.projects if ("projects", item.id) not in unknown]
        revisions.extend(
            _revision(f, "dropped", None) for f in findings if isinstance(f.target, ItemTarget)
        )

    # Changed fields.
    for finding in findings:
        target = finding.target
        if not isinstance(target, FieldTarget):
            continue
        original = next(
            (entry for entry in getattr(base, target.section) if entry.id == target.item_id), None
        )
        item = next(
            (entry for entry in getattr(resume, target.section) if entry.id == target.item_id),
            None,
        )
        if original is None or item is None:
            continue
        setattr(item, target.field, getattr(original, target.field))
        revisions.append(_revision(finding, "reverted", _value(getattr(original, target.field))))

    # Unknown skills.
    bad_skills = {f.target.index for f in findings if isinstance(f.target, SkillTarget)}
    if bad_skills:
        resume.skills = [skill for i, skill in enumerate(resume.skills) if i not in bad_skills]
        revisions.extend(
            _revision(f, "dropped", None) for f in findings if isinstance(f.target, SkillTarget)
        )

    # Lines. Keys index the pre-revision resume, which is `tailored`: the structures above only
    # removed whole items, never lines within them.
    by_key: dict[str, Finding] = {}
    for finding in findings:
        if isinstance(finding.target, TextTarget):
            by_key.setdefault(finding.target.key, finding)
    if not by_key:
        return resume, revisions

    # The summary: drop unsupported sentences; if none survive, use the base summary rather
    # than none at all.
    if resume.summary:
        sentences = split_sentences(resume.summary)
        kept: list[str] = []
        for index, sentence in enumerate(sentences):
            flagged_sentence = by_key.get(f"summary:sentence:{index}")
            if flagged_sentence is not None:
                revisions.append(_revision(flagged_sentence, "dropped", None))
            else:
                kept.append(sentence)
        if len(kept) < len(sentences):
            if kept:
                resume.summary = " ".join(kept)
            else:
                resume.summary = base.summary
                if revisions and base.summary:
                    revisions[-1].action = "replaced"
                    revisions[-1].replacement = base.summary

    def revise_bullets(
        section: ItemSection, item_id: str, highlights: list[str], originals: list[str]
    ) -> list[str]:
        def flagged(index: int) -> Finding | None:
            return by_key.get(f"{section}:{item_id}:highlight:{index}")

        # Originals already on the resume word for word are not candidates: putting one back
        # again would duplicate it.
        used = {normalize_text(line) for i, line in enumerate(highlights) if flagged(i) is None}
        lines: list[str] = []
        for index, line in enumerate(highlights):
            finding = flagged(index)
            if finding is None:
                lines.append(line)
                continue
            best: str | None = None
            best_score = MIN_RESEMBLANCE
            for original in originals:
                if normalize_text(original) in used:
                    continue
                score = similarity(line, original)
                if score >= best_score:
                    best = original
                    best_score = score
            if best is not None:
                used.add(normalize_text(best))
                lines.append(best)
                revisions.append(_revision(finding, "replaced", best))
            else:
                revisions.append(_revision(finding, "dropped", None))
        return lines

    base_work = {item.id: item for item in base.work}
    for item in resume.work:
        original = base_work.get(item.id)
        if original is None:
            continue
        summary_finding = by_key.get(f"work:{item.id}:summary:0")
        if summary_finding is not None:
            item.summary = original.summary
            action = "replaced" if original.summary else "dropped"
            revisions.append(_revision(summary_finding, action, original.summary))
        item.highlights = revise_bullets("work", item.id, item.highlights, original.highlights)

    base_projects = {item.id: item for item in base.projects}
    for item in resume.projects:
        original = base_projects.get(item.id)
        if original is None:
            continue
        description_finding = by_key.get(f"projects:{item.id}:description:0")
        if description_finding is not None:
            item.description = original.description
            action = "replaced" if original.description else "dropped"
            revisions.append(_revision(description_finding, action, original.description))
        item.highlights = revise_bullets("projects", item.id, item.highlights, original.highlights)

    base_education = {item.id: item for item in base.education}
    for item in resume.education:
        original = base_education.get(item.id)
        if original is None:
            continue
        item.highlights = revise_bullets("education", item.id, item.highlights, original.highlights)

    return resume, revisions


def _now_iso() -> str:
    """`new Date().toISOString()`."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def finalize_verification(
    *,
    base: Resume,
    final: Resume,
    result: VerifyResult,
    revisions: list[Revision],
    base_resume_id: str | None = None,
    base_version: int | None = None,
) -> Verification:
    """The stored verification: the deterministic re-check of the revised resume, plus the
    record of what was fixed to get there."""
    remaining = check_rules(base, final)
    return Verification(
        passed=not remaining,
        checked_at=_now_iso(),
        unsupported=[finding.public() for finding in remaining],
        revised=bool(revisions),
        checked=result.checked,
        model_checked=result.model_checked,
        revisions=revisions,
        model_error=result.model_error,
        base_resume_id=base_resume_id,
        base_version=base_version,
    )


def reconcile_changes(
    *,
    base: Resume,
    final: Resume,
    changes: list[TailorChange],
    revisions: list[Revision],
) -> list[TailorChange]:
    """The change log, made true to the saved resume.

    The tailor's own log describes its draft. Entries for lines the revision took back are
    removed, entries about items that are not on the resume are removed, evidence ids that are
    not base ids are dropped, and a rewrite the tailor forgot to log gets an entry: the log is
    the audit trail, so it has to cover every line that differs from the base."""
    known = set(fact_ids(base))
    reverted = {normalize_text(revision.text) for revision in revisions}

    final_claims = collect_claims(base, final)
    final_texts = {normalize_text(claim.text) for claim in final_claims}
    if final.summary:
        final_texts.add(normalize_text(final.summary))

    out: list[TailorChange] = []
    for change in changes:
        if change.item_id and change.item_id not in known:
            continue
        evidence = [fact for fact in change.evidence_fact_ids if fact in known]
        if change.section == "summary":
            # The summary is logged whole; after sentence-level revision it is re-stated from
            # the final text below.
            continue
        if change.after:
            after = normalize_text(change.after)
            if after in reverted:
                continue
            if change.section != "skills" and after not in final_texts:
                continue
        out.append(
            change.model_copy(
                update={
                    "evidence_fact_ids": evidence or ([change.item_id] if change.item_id else [])
                }
            )
        )

    # The summary, as it finally stands.
    final_summary = final.summary or ""
    if final_summary != "" and normalize_text(final_summary) != normalize_text(base.summary or ""):
        logged = next((change for change in changes if change.section == "summary"), None)
        out.insert(
            0,
            TailorChange(
                section="summary",
                item_id=None,
                before=base.summary,
                after=final.summary,
                reason=(logged.reason if logged is not None and logged.reason else None)
                or "Rewritten for this posting from facts on your resume.",
                evidence_fact_ids=[
                    fact for fact in (logged.evidence_fact_ids if logged else []) if fact in known
                ],
            ),
        )

    # Rewrites the tailor did not log.
    logged_texts = {normalize_text(change.after) for change in out if change.after}
    for claim in final_claims:
        if claim.verbatim or claim.field == "sentence":
            continue
        if normalize_text(claim.text) in logged_texts:
            continue
        out.append(
            TailorChange(
                section=claim.section,
                item_id=claim.item_id,
                before=_closest_original(base, claim),
                after=claim.text,
                reason="Reworded for this posting.",
                evidence_fact_ids=[claim.item_id] if claim.item_id else [],
            )
        )

    return out


def _closest_original(base: Resume, claim: Claim) -> str | None:
    item = next(
        (
            entry
            for entry in [*base.work, *base.projects, *base.education]
            if entry.id == claim.item_id
        ),
        None,
    )
    if item is None:
        return None
    if claim.field == "summary":
        return getattr(item, "summary", None)
    if claim.field == "description":
        return getattr(item, "description", None)
    best: str | None = None
    best_score = 0.0
    for original in item.highlights:
        score = similarity(claim.text, original)
        if score > best_score:
            best = original
            best_score = score
    return best
