"""The JavaScript string and regex semantics the ported sources rely on (design §10.3).

The sources are literal ports of `src/lib/sources/*.ts`, pinned by goldens captured from the
TypeScript functions. The traps (`research/documents-pdf.md` §4):

- JS `\\b`, `\\w` and `\\d` are ASCII-only, while JS `\\s` is Unicode. Patterns are compiled
  with `re.ASCII` (which narrows `\\b`, `\\w`, `\\d` and case folding) and every `\\s` is
  spelled out as `S`, the JS whitespace class, so `re.ASCII` cannot narrow it.
- `re.ASCII | re.IGNORECASE` reproduces JS's non-`u` case folding (no `ſ` to `s`, no KELVIN
  SIGN to `k`).
- JS `$` (no `m` flag) is the end of input: Python's `\\Z`, never `$`.
- JS `.` does not match any line terminator: `NOT_LT`, never `.`.
- `.trim()` strips the JS whitespace class, which is not `str.strip()`'s.

String lengths are Python-native (code points), which differ from JS only on astral
characters (design §10.3).
"""

from __future__ import annotations

import math
import re
from typing import Any
from urllib.parse import quote

# WhiteSpace and LineTerminator in ECMAScript: what `\s` matches and `.trim()` strips.
JS_WS_CHARS = (
    "\t\n\v\f\r \u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000\ufeff"
)
# The same set as the inside of a regex character class.
JS_WS = "\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
S = f"[{JS_WS}]"
NOT_LT = "[^\n\r\u2028\u2029]"  # JS `.`

# `\b`, `\w`, `\d` and case folding as in a JS regex with the `i` flag (and no `u`).
IA = re.IGNORECASE | re.ASCII


def js_trim(value: str) -> str:
    return value.strip(JS_WS_CHARS)


def js_truthy(value: Any) -> bool:
    """JavaScript truthiness: `null`, `undefined`, `false`, `0`, `NaN` and `""` are falsy, and
    every array and object is truthy (unlike Python's empty containers)."""
    if value is None or value is False:
        return False
    if isinstance(value, int | float):
        return not (value == 0 or (isinstance(value, float) and math.isnan(value)))
    if isinstance(value, str):
        return value != ""
    return True


def js_lower(value: Any) -> str | None:
    """`value?.toLowerCase()` for a field that should be a string."""
    return value.lower() if isinstance(value, str) else None


def encode_uri_component(value: str) -> str:
    """`encodeURIComponent`: everything but `A-Z a-z 0-9 - _ . ! ~ * ' ( )` is escaped."""
    return quote(value, safe="-_.!~*'()")
