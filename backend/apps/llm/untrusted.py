"""Third-party text on its way to a model (design §7.2, Known risks; #30).

A posting, a careers page or a company name was written by someone other than the user, so a
model reads it inside `<untrusted source="…">` tags, and every prompt that carries such text
says those tags hold data, never instructions. Two things keep the tags meaningful:

- **No forged tags.** `neutralize()` defangs any `<untrusted` or `</untrusted` inside the text,
  so a posting cannot close its wrapper early and continue as if it were the prompt.
- **Short fields stay on their line.** `one_line()` folds line breaks and control characters in
  a title or a company name into spaces, so a field cannot start a line of its own ("Title:
  …\\nRules: …") inside the block it sits in.

`untrusted(text, source)` does both and wraps; `field(text)` wraps one short value inline, for
JSON a model reads (the chat's tool results, `apps/chat/model_view.py`).
"""

from __future__ import annotations

import re
from typing import Final

__all__ = [
    "JOB_POSTING",
    "field",
    "neutralize",
    "one_line",
    "untrusted",
]

JOB_POSTING: Final = "job posting"

# `<untrusted`, `</untrusted`, `< / UNTRUSTED`: anything a model could read as the tag.
_TAG = re.compile(r"<(\s*/?\s*)(untrusted)", re.IGNORECASE)
# Line and paragraph breaks and the other control characters (tab included).
_BREAKS = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029]+")


def neutralize(text: str) -> str:
    """The text with every wrapper tag defanged (`</untrusted>` reads `[/untrusted>`)."""
    return _TAG.sub(r"[\1\2", text)


def one_line(text: str) -> str:
    """A short field on one line: breaks and control characters become one space each run."""
    return _BREAKS.sub(" ", text)


def untrusted(text: str, source: str = JOB_POSTING) -> str:
    """A block of third-party text, wrapped, on lines of its own."""
    return f'<untrusted source="{source}">\n{neutralize(text)}\n</untrusted>'


def field(text: str) -> str:
    """One short third-party value, wrapped inline: `<untrusted>Staff Engineer</untrusted>`."""
    return f"<untrusted>{neutralize(one_line(text))}</untrusted>"
