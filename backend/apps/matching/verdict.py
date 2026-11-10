"""What a match leads with beside its recommendation (design "Interface"; #4, #38): one line of
why, and a blocker when there is one. Both are read from the two stored evaluations, so a row
and the match page say the same thing and neither costs a query.

- **Why**: the first sentence of the deep review's `summary`, else the fast score's first
  `fit_reasons` entry.
- **Blocker**: the review's first gap rated `disqualifying`; for a match with no review, the fast
  score's first `hard_requirement_misses` entry. A review overrules triage: once one is stored,
  a miss only the cheap pass saw is not shown beside the review's verdict.

The evaluations are read as stored JSON, leniently: an import or an older version may have left
anything there, and a row must still render.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "APPLY_AT",
    "MAYBE_AT",
    "STRONG_AT",
    "band_of",
    "blocker_for",
    "first_sentence",
    "reason_for",
]

# The deep review's bands (`deep_eval.RUBRIC`), which are the only numbers anything in the app
# may call a score "strong" or "worth applying to" by (#87): a row's verdict, Home's count of
# strong matches and a run's summary all read them from here.
STRONG_AT = 85
APPLY_AT = 70
MAYBE_AT = 50


def band_of(score: int) -> str:
    """A 0-100 score as the rubric's recommendation."""
    if score >= STRONG_AT:
        return "strong"
    if score >= APPLY_AT:
        return "apply"
    return "maybe" if score >= MAYBE_AT else "skip"


# A sentence ends at ".", "!" or "?" (with any closing quote or bracket) before a space and a
# capital or a digit, so "Node.js", "3.5 years" and "e.g. the API" do not end one.
_SENTENCE_END = re.compile(r"([.!?][\"'”’)\]]*) (?=[\"'“‘(\[]?[A-Z0-9])")
# Words whose full stop is not a sentence's, even before a capital: "U.S. Citizens", "Sr. Staff".
_ABBREVIATIONS = frozenset(
    {
        "approx.",
        "co.",
        "dr.",
        "e.g.",
        "etc.",
        "i.e.",
        "inc.",
        "jr.",
        "ltd.",
        "mr.",
        "mrs.",
        "ms.",
        "no.",
        "sr.",
        "st.",
        "u.k.",
        "u.s.",
        "vs.",
    }
)


def _line(value: Any) -> str | None:
    """A stored string on one line; None when it is not a string or says nothing."""
    if not isinstance(value, str):
        return None
    return " ".join(value.split()) or None


def _first(items: Any) -> str | None:
    """The first entry of a stored list that says something."""
    if not isinstance(items, list):
        return None
    return next((line for line in map(_line, items) if line is not None), None)


def first_sentence(text: str) -> str:
    """The first sentence of a paragraph, on one line; the whole of it when nothing ends one."""
    line = " ".join(text.split())
    for end in _SENTENCE_END.finditer(line):
        sentence = line[: end.end(1)]
        if sentence.rsplit(" ", 1)[-1].lower() not in _ABBREVIATIONS:
            return sentence
    return line


def reason_for(deep_eval: Any, fast_eval: Any) -> str | None:
    """One line on why the match fits, or None when neither pass has said."""
    if isinstance(deep_eval, dict):
        summary = _line(deep_eval.get("summary"))
        if summary is not None:
            return first_sentence(summary)
    return _first(fast_eval.get("fit_reasons")) if isinstance(fast_eval, dict) else None


def blocker_for(deep_eval: Any, fast_eval: Any) -> str | None:
    """What would rule the match out, in a few words, or None."""
    if isinstance(deep_eval, dict):
        gaps = deep_eval.get("gaps")
        return _first(
            [
                gap.get("gap")
                for gap in (gaps if isinstance(gaps, list) else [])
                if isinstance(gap, dict) and gap.get("severity") == "disqualifying"
            ]
        )
    return _first(fast_eval.get("hard_requirement_misses")) if isinstance(fast_eval, dict) else None
