"""`resume_to_text`, the prose models read (design §11.3), against the TS goldens."""

from __future__ import annotations

from typing import Any

import pytest

from apps.resumes.text import RESUME_SECTIONS, js_trim, resume_to_text
from tests.resumes.support import golden, resume


@pytest.mark.parametrize("case", golden("strings/resume_text.json"), ids=lambda case: case["name"])
def test_matches_the_ts_prose_byte_for_byte(case: dict[str, Any]):
    sections = case["sections"] if case["sections"] is not None else RESUME_SECTIONS
    assert resume_to_text(resume(case["resume"]), sections) == case["output"]


def test_an_empty_string_is_not_a_missing_value():
    """`??` keeps an empty string: only a missing date prints "?"."""
    text = resume_to_text(
        resume(
            {
                "basics": {"name": ""},
                "education": [{"id": "e1", "institution": "Bellwater", "startDate": None}],
            }
        )
    )
    assert text == "Name: (unknown)\n\nEDUCATION\n[e1]   — Bellwater (? – ?)"
    blank = resume(
        {"basics": {"name": "A"}, "work": [{"id": "w1", "company": "C", "position": "P"}]}
    )
    blank.work[0].start_date = ""  # not something the schema accepts from a request
    assert "\n   – present · source: resume" in resume_to_text(blank)


def test_js_trim_uses_javascript_whitespace():
    assert js_trim("\ufeff x \xa0") == "x"
    assert js_trim("\x1c x \x85") == "\x1c x \x85"  # Python's strip() would remove these
