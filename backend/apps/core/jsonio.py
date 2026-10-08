"""JavaScript-compatible formatting for model input (design §8.10).

Prompts are pinned byte for byte against goldens captured from the TypeScript builders, so every
number and JSON blob that reaches a prompt or a tool string goes through these helpers, never
`str()` or an f-string on a float.
"""

from __future__ import annotations

import json
import math
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

_JS_EXPONENT_LIMIT = 1e21  # JS switches to exponent notation at 1e21 and below 1e-6


def js_num(x: float | int) -> str:
    """What `String(x)` prints in JS: `150000` for an integral float, else the shortest
    round-trip form (`124.128`)."""
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
    if x.is_integer() and abs(x) < _JS_EXPONENT_LIMIT:
        return str(int(x))
    text = repr(x)
    if "e" in text:  # 1e+21 and 1e-07 in Python are 1e+21 and 1e-7 in JS
        mantissa, exponent = text.split("e")
        sign = "-" if exponent.startswith("-") else "+"
        return f"{mantissa}e{sign}{int(exponent.lstrip('+-'))}"
    return text


def js_locale(x: float | int) -> str:
    """What `x.toLocaleString("en-US")` prints: thousands separators, at most three fraction
    digits, rounded half away from zero, trailing zeros dropped (`150,000`, `124.128`)."""
    if isinstance(x, bool):
        raise TypeError("js_locale() takes a number, not a bool")
    if isinstance(x, float):
        if math.isnan(x):
            return "NaN"
        if math.isinf(x):
            return "∞" if x > 0 else "-∞"
    # ICU formats the shortest round-trip decimal, then rounds it (halfExpand).
    value = Decimal(repr(x)) if isinstance(x, float) else Decimal(x)
    rounded = value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    sign = "-" if rounded < 0 else ""
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
