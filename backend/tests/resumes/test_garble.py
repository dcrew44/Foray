"""Garble detection (design §11.2): today's thresholds and reason strings, counted the Python
way."""

from __future__ import annotations

from typing import Any

import pytest

from apps.resumes.parse.garble import detect_garble
from tests.resumes.support import golden


@pytest.mark.parametrize(
    "case",
    golden("resumes/garble_cases.json"),
    ids=lambda case: f"{case['input']['text'][:24]!r}x{case['input']['pageCount']}",
)
def test_matches_the_ts_decisions_metrics_and_reasons(case: dict[str, Any]):
    report = detect_garble(case["input"]["text"], case["input"]["pageCount"])
    assert report.dump() == case["output"]


def test_metrics_serialise_as_javascript_numbers():
    report = detect_garble("word " * 200, 1)
    assert report.dump()["metrics"]["charsPerPage"] == 1000
    assert isinstance(report.dump()["metrics"]["charsPerPage"], int)


def test_lengths_are_code_points():
    """An astral letter is one character here (two UTF-16 units in TS), an accepted parity gap
    (design §10.3)."""
    text = "\U0001d400" * 200
    report = detect_garble(text, 1)
    assert report.metrics["chars"] == 200
    assert report.metrics["alphaRatio"] == 1


def test_whitespace_is_javascripts():
    """U+FEFF is whitespace in JS (and here); U+001C is not, and is a control character."""
    assert detect_garble("a\ufeffb", 1).metrics["spaceRatio"] == pytest.approx(1 / 3)
    report = detect_garble("a\x1cb", 1)
    assert report.metrics["spaceRatio"] == 0
    assert "replacement or control characters — broken font encoding" in report.reasons


def test_reasons_round_like_javascript():
    # Math.round(2.5) is 3 (Python's round() would say 2); toFixed(3) of 159/2159 is 0.074.
    assert detect_garble("a b c", 2).reasons[0].startswith("only 3 characters per page")
    report = detect_garble("x" * 2000 + " " * 159, 1)
    assert report.reasons == ["word spacing lost (0.074 of characters are whitespace)"]
