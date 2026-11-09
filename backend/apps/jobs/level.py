"""The words that mark a title's level (the prefilter's guard and the radar's tiers share them).

A title carrying an early-career word is never excluded from a junior search
(`matching.prefilter.guard_seniority`), and a board that lists one is polled every fifteen
minutes instead of hourly (`registry.due_companies`, #55).
"""

from __future__ import annotations

import re

__all__ = [
    "EARLY_CAREER_WORDS",
    "SENIOR_WORDS",
    "is_early_career_title",
    "word_pattern",
]

# JavaScript without the `u` flag: ASCII word classes and case folding.
_FLAGS = re.IGNORECASE | re.ASCII


def word_pattern(phrase: str) -> str:
    """A word or phrase bounded by non-letters, written to mean the same in Postgres and
    JavaScript (and Python, case-insensitively)."""
    return f"(^|[^a-z]){phrase.replace(' ', '[^a-z]+')}([^a-z]|$)"


# Early-career title words. A title carrying one is never excluded from a junior search.
EARLY_CAREER_WORDS = (
    "intern",
    "internship",
    "new grad",
    "new graduate",
    "graduate program",
    "junior",
    "jr",
    "entry level",
    "associate",
    "early career",
    "university grad",
    "resident",
    "residency",
    "apprentice",
    "apprenticeship",
    "fellow",
    "fellows",
    "fellowship",
    "rotational",
)

SENIOR_WORDS = (
    "senior",
    "sr",
    "staff",
    "principal",
    "lead",
    "manager",
    "director",
    "head of",
    "vice president",
    "vp",
    "distinguished",
)

_EARLY_CAREER = re.compile("|".join(word_pattern(word) for word in EARLY_CAREER_WORDS), _FLAGS)


def is_early_career_title(title: str) -> bool:
    return _EARLY_CAREER.search(title) is not None
