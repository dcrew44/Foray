"""The JS-compatible formatters of `apps.core.jsonio` (design §8.10), against JavaScript itself.

Every expected value below was printed by Node 26 (`String(x)` and `x.toLocaleString("en-US")`).
Prompts and tool strings use these helpers, never `str()` on a float, because a byte change in a
prompt shifts behaviour and breaks prompt-cache prefixes without failing anything else.

The cases outside the salary domain (small fractions, integral floats above 2**53, a negative
value that rounds to zero, the largest double) were strict xfails until step H1 made the module
follow ECMAScript's Number::toString and ICU's sign rule (the migration's step-H1 notes, removed
with docs/migration/ at the merge; see `git log -- docs/migration/deviations/step-H1.md`).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from apps.core.jsonio import js_json, js_json_indent, js_locale, js_num

GOLDENS = Path(__file__).resolve().parents[1] / "goldens"

# (x, String(x), x.toLocaleString("en-US"))
CASES: list[tuple[float | int, str, str]] = [
    (0, "0", "0"),
    (-0.0, "0", "-0"),
    (1, "1", "1"),
    (-1, "-1", "-1"),
    (150000, "150000", "150,000"),
    (150000.0, "150000", "150,000"),
    (124.128, "124.128", "124.128"),
    (130.5, "130.5", "130.5"),
    (7.982, "7.982", "7.982"),
    (72.5, "72.5", "72.5"),
    (0.1, "0.1", "0.1"),
    (0.30000000000000004, "0.30000000000000004", "0.3"),
    (1 / 3, "0.3333333333333333", "0.333"),
    (2 / 3, "0.6666666666666666", "0.667"),
    (12345.6789, "12345.6789", "12,345.679"),
    (1234567.891, "1234567.891", "1,234,567.891"),
    (-1234.5, "-1234.5", "-1,234.5"),
    (0.0005, "0.0005", "0.001"),
    (0.0015, "0.0015", "0.002"),
    (1.0005, "1.0005", "1.001"),
    (2.0005, "2.0005", "2.001"),
    (999.9995, "999.9995", "1,000"),
    (100.125, "100.125", "100.125"),
    (0.125, "0.125", "0.125"),
    (1.0004999, "1.0004999", "1"),
    (0.0001, "0.0001", "0"),
    (0.000123, "0.000123", "0"),
    (1e-7, "1e-7", "0"),
    (1.5e-7, "1.5e-7", "0"),
    (5e-324, "5e-324", "0"),
    (1e20, "100000000000000000000", "100,000,000,000,000,000,000"),
    (1e21, "1e+21", "1,000,000,000,000,000,000,000"),
    (4.35e17, "435000000000000000", "435,000,000,000,000,000"),
    (1e15 + 0.3, "1000000000000000.2", "1,000,000,000,000,000.2"),
    (9007199254740993.0, "9007199254740992", "9,007,199,254,740,992"),
    (float(2**53 + 2), "9007199254740994", "9,007,199,254,740,994"),
    (math.inf, "Infinity", "∞"),
    (-math.inf, "-Infinity", "-∞"),
]


@pytest.mark.parametrize(("x", "string", "_locale"), CASES)
def test_js_num(x: float, string: str, _locale: str) -> None:
    assert js_num(x) == string


@pytest.mark.parametrize(
    ("x", "_string", "locale"),
    [case for case in CASES if case[2] != "-0"],  # see test_js_locale_negative_zero
)
def test_js_locale(x: float, _string: str, locale: str) -> None:
    assert js_locale(x) == locale


def test_nan() -> None:
    assert js_num(math.nan) == "NaN"
    assert js_locale(math.nan) == "NaN"


def test_bools_are_refused() -> None:
    with pytest.raises(TypeError):
        js_num(True)
    with pytest.raises(TypeError):
        js_locale(False)


# --- outside the salary domain ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("x", "string"),
    [
        (1e-6, "0.000001"),  # JS prints fixed notation down to 1e-6; Python's repr from 1e-4
        (1e-5, "0.00001"),
        (5e-5, "0.00005"),
        (1.5e-6, "0.0000015"),
    ],
)
def test_js_num_small_fractions(x: float, string: str) -> None:
    assert js_num(x) == string


@pytest.mark.parametrize(
    ("x", "string"),
    [
        # JS prints the shortest round-trip digits, padded with zeros; str(int(x)) the exact value
        (123456789012345680000.0, "123456789012345680000"),
        (1.2345678901234567e19, "12345678901234567000"),
    ],
)
def test_js_num_integral_floats_above_2_53(x: float, string: str) -> None:
    assert js_num(x) == string


@pytest.mark.parametrize("x", [-0.0, -2.5e-8, -0.0004])
def test_js_locale_negative_zero(x: float) -> None:
    assert js_locale(x) == "-0"


def test_js_locale_huge_values() -> None:
    expected = "179,769,313,486,231,570" + ",000" * 97
    assert js_locale(1.7976931348623157e308) == expected


# --- JSON, as JSON.stringify writes it -----------------------------------------------------------


def test_js_json_is_compact_and_keeps_unicode_and_key_order() -> None:
    value = {"b": 1, "a": ["é", None, True, 1.5], "c": {"z": "–"}}
    assert js_json(value) == '{"b":1,"a":["é",null,true,1.5],"c":{"z":"–"}}'


def test_js_json_indent() -> None:
    assert js_json_indent({"a": [1, "é"]}, 2) == '{\n  "a": [\n    1,\n    "é"\n  ]\n}'
    assert js_json_indent({}, 2) == "{}"


# --- the salary strings the goldens pin ----------------------------------------------------------


def test_salary_goldens_use_these_formats() -> None:
    """`get_job`'s salary string (`tool_salary` golden) prints each bound as JS `String()` did."""
    golden = json.loads((GOLDENS / "prompts" / "tool_salary" / "get_job.json").read_text())
    for case in golden["cases"]:
        raw = case["input"]
        if case["output"]["salary"] is None:  # no bounds at all: no salary line
            assert raw["salary_min"] is None and raw["salary_max"] is None
            continue
        low = js_num(float(raw["salary_min"])) if raw["salary_min"] is not None else "?"
        high = js_num(float(raw["salary_max"])) if raw["salary_max"] is not None else "?"
        currency = raw["salary_currency"]
        period = raw["salary_period"] or "?"
        text = f"{currency + ' ' if currency else ''}{low}–{high} per {period}"
        assert text == case["output"]["salary"]
