"""JavaScript-compatible formatting for model input (design §8.10).

Prompts are pinned byte for byte against goldens captured from the TypeScript builders, so every
number and JSON blob that reaches a prompt or a tool string goes through these helpers, never
`str()` or an f-string on a float.
"""

from __future__ import annotations

import json
import math
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any

# Enough significant digits to quantize any finite double to 0.001 exactly: the largest has 309
# integer digits.
_LOCALE_PRECISION = 400


def _shortest_digits(x: float) -> tuple[str, int]:
    """For a finite, non-zero `|x|`: the digits `s` and exponent `n` of ECMAScript's
    Number::toString (`x = 0.s × 10**n`, `s` as short as round-tripping allows). `repr` gives
    the same shortest round-trip digits that JavaScript picks."""
    _, digits, exponent = Decimal(repr(abs(x))).as_tuple()
    assert isinstance(exponent, int)  # finite
    # repr may carry trailing zeros ("150000.0"): they move into the exponent, not the digits.
    return "".join(map(str, digits)).rstrip("0"), exponent + len(digits)


def js_num(x: float | int) -> str:
    """What `String(x)` prints in JS (ECMAScript Number::toString): `150000` for an integral
    float, else the shortest round-trip form (`124.128`); plain notation from 1e-6 up to 1e21,
    exponent notation (`1e-7`, `1e+21`) outside it."""
    if isinstance(x, bool):
        raise TypeError("js_num() takes a number, not a bool")
    if isinstance(x, int):
        return str(x)
    if math.isnan(x):
        return "NaN"
    if math.isinf(x):
        return "Infinity" if x > 0 else "-Infinity"
    if x == 0:
        return "0"  # JS prints -0 as "0"
    sign = "-" if x < 0 else ""
    digits, n = _shortest_digits(x)
    k = len(digits)
    if k <= n <= 21:  # an integer: the digits, then zeros
        return f"{sign}{digits}{'0' * (n - k)}"
    if 0 < n <= 21:  # the point falls inside the digits
        return f"{sign}{digits[:n]}.{digits[n:]}"
    if -6 < n <= 0:  # a small fraction, still in plain notation
        return f"{sign}0.{'0' * -n}{digits}"
    exponent = n - 1
    mantissa = digits if k == 1 else f"{digits[0]}.{digits[1:]}"
    return f"{sign}{mantissa}e{'+' if exponent > 0 else '-'}{abs(exponent)}"


def js_locale(x: float | int) -> str:
    """What `x.toLocaleString("en-US")` prints: thousands separators, at most three fraction
    digits, rounded half away from zero, trailing zeros dropped (`150,000`, `124.128`). A
    negative value keeps its sign even when it rounds to zero (`-0`), as ICU does."""
    if isinstance(x, bool):
        raise TypeError("js_locale() takes a number, not a bool")
    if isinstance(x, float):
        if math.isnan(x):
            return "NaN"
        if math.isinf(x):
            return "∞" if x > 0 else "-∞"
    # ICU formats the shortest round-trip decimal, then rounds it (halfExpand).
    value = Decimal(repr(x)) if isinstance(x, float) else Decimal(x)
    with localcontext() as context:
        context.prec = _LOCALE_PRECISION
        rounded = value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    sign = "-" if value.is_signed() else ""  # -0.0 and -0.0004 included
    integral, _, fraction = f"{abs(rounded):f}".partition(".")
    fraction = fraction.rstrip("0")
    grouped = f"{int(integral):,}"
    return f"{sign}{grouped}.{fraction}" if fraction else f"{sign}{grouped}"


def js_json(x: Any) -> str:
    """`JSON.stringify(x)`: compact, non-ASCII kept, keys in insertion (schema) order."""
    return json.dumps(x, separators=(",", ":"), ensure_ascii=False)


def js_json_indent(x: Any, indent: int = 2) -> str:
    """`JSON.stringify(x, null, indent)`."""
    return json.dumps(x, indent=indent, ensure_ascii=False)
