"""The careers-page listing prompt against the TypeScript goldens, byte for byte (design §8.10).

`ALLOWED_DIFFS` lists every declared difference with its reason; nothing else may differ.
"""

from __future__ import annotations

import pytest

from apps.jobs.sources.careers_page import (
    LISTING_INSTRUCTIONS,
    MAX_LISTING_CHARS,
    listing_prompt,
)
from apps.jobs.sources.clean import html_to_text_with_links
from tests.jobs.conftest import fixture_text, golden

ALLOWED_DIFFS = {
    # §7.2, §8.10, §10.5 (bug #90): page text reaches the model wrapped as third-party data.
    "careers_listing": [
        ("\n\n<page>\n", '\n\n<untrusted source="careers page">\n'),
        ("\n</page>", "\n</untrusted>"),
    ],
}


def _declared(builder: str, text: str) -> str:
    for before, after in ALLOWED_DIFFS.get(builder, []):
        assert text.count(before) == 1, (builder, before)
        text = text.replace(before, after)
    return text


@pytest.mark.parametrize("case", ["retool", "crusoe"])
def test_careers_listing_prompt_matches_golden(case: str) -> None:
    expected = golden(f"prompts/careers_listing/{case}.json")
    company = expected["input"]["company"]
    url = company["careersUrls"][0]
    page = fixture_text(expected["input"]["fixture"])

    text = html_to_text_with_links(page, url)[:MAX_LISTING_CHARS]

    assert expected["instructions"] == LISTING_INSTRUCTIONS
    assert listing_prompt(company["name"], url, text) == _declared(
        "careers_listing", expected["prompt"]
    )
