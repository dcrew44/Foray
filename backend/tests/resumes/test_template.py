"""The HTML template (design §11.5; port of `resume-template.test.ts`, 16 cases, plus the template
goldens).

The template is what recruiters and their parsers see. These pin that it is a complete, safe
document with the sections in the agreed order, that every value is escaped (the same HTML is
served to the browser for the preview), and that stored text, dates especially, comes out
exactly as stored.

ALLOWED_DIFFS (against `goldens/template/*.html`): Django's autoescape writes `'` as `&#x27;`
where the TS template wrote `&#39;`. Both are the same character to every HTML parser.
"""

from __future__ import annotations

import functools
import re
from typing import Any

import pytest
from django.utils.html import escape

from apps.resumes.template import date_range, display_url, render_resume_html, safe_href
from tests.resumes.support import golden, golden_text, resume

ALLOWED_DIFFS = {"&#x27;": "&#39;"}


def as_ts(html: str) -> str:
    for ours, theirs in ALLOWED_DIFFS.items():
        html = html.replace(ours, theirs)
    return html


def sample(**overrides: Any):
    data: dict[str, Any] = {
        "basics": {
            "name": "Dana Reyes",
            "email": "dana@example.com",
            "phone": "(555) 0134-2087",
            "location": "Sacramento, CA",
            "links": [
                {"id": "l1", "url": "github.com/dana", "label": "GitHub"},
                {"id": "l2", "url": "https://www.linkedin.com/in/dana/", "label": "LinkedIn"},
            ],
        },
        "summary": "Backend-leaning engineer.",
        "work": [
            {
                "id": "w1",
                "company": "Kestrel Logistics Group",
                "position": "Software Engineer II",
                "location": "Sacramento, CA",
                "startDate": "March 2023",
                "endDate": "Present",
                "highlights": ["Cut latency from 840 ms to 190 ms."],
            }
        ],
        "education": [
            {
                "id": "e1",
                "institution": "Bellwater State University",
                "studyType": "B.S.",
                "area": "Computer Science",
                "startDate": "2017",
                "endDate": "2021",
                "highlights": [],
            }
        ],
        "projects": [
            {
                "id": "p1",
                "name": "Tideline",
                "url": "github.com/dana/tideline",
                "description": "A CLI.",
            }
        ],
        "skills": [
            {"id": "s1", "name": "TypeScript", "category": "Languages"},
            {"id": "s2", "name": "PostgreSQL", "category": "Data"},
            {"id": "s3", "name": "Python", "category": "Languages"},
        ],
    }
    data.update(overrides)
    return resume(data)


# --- escaping ------------------------------------------------------------------------------------


def test_escapes_every_character_that_can_break_out():
    assert as_ts(escape("<a href=\"x\" onclick='y'>&</a>")) == (
        "&lt;a href=&quot;x&quot; onclick=&#39;y&#39;&gt;&amp;&lt;/a&gt;"
    )


def test_missing_values_render_empty():
    html = render_resume_html(resume({"basics": {"name": "Zoë"}, "work": []}))
    assert "None" not in html
    assert '<p class="headline">' not in html
    assert '<p class="contact">' not in html


# --- safe_href -----------------------------------------------------------------------------------


def test_safe_href_adds_https_and_keeps_http_and_mailto():
    assert safe_href("github.com/dana") == "https://github.com/dana"
    assert safe_href("https://example.com/x") == "https://example.com/x"
    assert safe_href("dana@example.com") == "mailto:dana@example.com"
    assert safe_href("mailto:dana@example.com") == "mailto:dana@example.com"


def test_safe_href_refuses_other_schemes_and_junk():
    assert safe_href("javascript:alert(1)") is None
    assert safe_href("data:text/html,<script>") is None
    assert safe_href("not a url") is None
    assert safe_href("") is None


# --- helpers -------------------------------------------------------------------------------------


def test_display_url_reads_naturally():
    assert display_url("https://www.linkedin.com/in/dana/") == "linkedin.com/in/dana"


def test_date_range_keeps_dates_as_stored():
    assert date_range("March 2023", "Present") == "March 2023 – Present"
    assert date_range("2021", None) == "2021"
    assert date_range(None, "June 2024") == "June 2024"
    assert date_range(None, None) == ""


# --- render_resume_html --------------------------------------------------------------------------


@functools.cache
def html() -> str:
    return render_resume_html(sample())


def test_is_a_complete_document_with_inline_css_and_a_letter_page():
    doc = html()
    assert doc.startswith("<!doctype html>")
    assert "<style>" in doc
    assert "@page { size: Letter; margin: 0.5in 0.6in; }" in doc
    assert not re.search(r"<script|<link |<img ", doc, re.IGNORECASE)
    assert "<title>Dana Reyes — Resume</title>" in doc


def test_name_first_and_sections_in_order():
    doc = html()
    markers = ["Dana Reyes", ">Summary<", ">Experience<", ">Projects<", ">Education<", ">Skills<"]
    order = [doc.find(marker) for marker in markers]
    assert all(index >= 0 for index in order)
    assert sorted(order) == order


def test_contact_details_are_readable_text_with_working_links():
    doc = html()
    assert '<a href="mailto:dana@example.com">dana@example.com</a>' in doc
    assert '<a href="https://github.com/dana">github.com/dana</a>' in doc
    assert '<a href="https://www.linkedin.com/in/dana/">linkedin.com/in/dana</a>' in doc
    assert 'href="tel:5550134' in doc


def test_keeps_stored_text_verbatim():
    doc = html()
    assert "March 2023 – Present" in doc
    assert "Software Engineer II" in doc
    assert "Cut latency from 840 ms to 190 ms." in doc
    assert "B.S. in Computer Science" in doc


def test_groups_skills_by_category_in_order_of_first_appearance():
    doc = html()
    assert re.search(r"Languages:</span> TypeScript, Python", doc)
    assert doc.index("Languages:") < doc.index("Data:")


def test_uncategorised_skills_on_one_plain_line():
    plain = render_resume_html(
        sample(
            skills=[
                {"id": "a", "source": "resume", "name": "Go", "category": None},
                {"id": "b", "source": "resume", "name": "Rust", "category": None},
            ]
        )
    )
    assert '<p class="skills-line">Go, Rust</p>' in plain


def test_escapes_hostile_text_everywhere():
    hostile = render_resume_html(
        sample(
            summary="<img src=x onerror=alert(1)>",
            work=[
                {
                    "id": "w1",
                    "source": "resume",
                    "company": 'Acme "Corp"',
                    "position": "<b>Boss</b>",
                    "startDate": "2020",
                    "endDate": "2021",
                    "highlights": ["<script>alert('x')</script>"],
                }
            ],
            basics={
                "name": "</title><script>alert(1)</script>",
                "links": [
                    {"id": "l", "source": "resume", "url": "javascript:alert(1)", "label": "x"}
                ],
            },
        )
    )
    assert "<script>" not in hostile
    assert "<img" not in hostile
    assert "<b>Boss" not in hostile
    assert "&lt;script&gt;alert(&#39;x&#39;)&lt;/script&gt;" in as_ts(hostile)
    assert "Acme &quot;Corp&quot;" in hostile
    assert 'href="javascript:' not in hostile


def test_leaves_out_empty_sections():
    sparse = render_resume_html(sample(summary=None, projects=[], skills=[], education=[]))
    assert ">Summary<" not in sparse
    assert ">Projects<" not in sparse
    assert ">Skills<" not in sparse
    assert ">Experience<" in sparse


def test_compact_density_changes_no_content():
    compact = render_resume_html(sample(), density="compact")
    assert '<main class="page compact">' in compact

    def text(doc: str) -> str:
        return re.sub(r"<style>[\s\S]*</style>", "", doc).replace(" compact", "", 1)

    assert text(compact) == text(html())


def test_takes_a_custom_title():
    assert "<title>Custom</title>" in render_resume_html(sample(), title="Custom")


# --- goldens -------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", golden("template/cases.json"), ids=lambda case: case["name"])
def test_matches_the_ts_template_byte_for_byte(case: dict[str, Any]):
    options = case["options"]
    rendered = render_resume_html(
        resume(case["resume"]),
        title=options.get("title"),
        density=options.get("density", "normal"),
    )
    assert as_ts(rendered) == golden_text(f"template/{case['html']}")


HELPERS = golden("template/helpers.json")


@pytest.mark.parametrize("case", HELPERS["safeHref"], ids=lambda case: repr(case["input"]))
def test_safe_href_golden(case: dict[str, Any]):
    assert safe_href(case["input"]) == case["output"]


@pytest.mark.parametrize("case", HELPERS["displayUrl"], ids=lambda case: repr(case["input"]))
def test_display_url_golden(case: dict[str, Any]):
    assert display_url(case["input"]) == case["output"]


@pytest.mark.parametrize("case", HELPERS["dateRange"], ids=lambda case: repr(case["input"]))
def test_date_range_golden(case: dict[str, Any]):
    assert date_range(*case["input"]) == case["output"]


@pytest.mark.parametrize(
    "case",
    [case for case in HELPERS["escapeHtml"] if case["input"] not in (None, "__undefined__")],
    ids=lambda case: repr(case["input"]),
)
def test_escaping_golden(case: dict[str, Any]):
    """What autoescape writes for a value, against `escapeHtml` (None never reaches the
    template: the context builder turns a missing value into an empty string or no element)."""
    assert as_ts(escape(case["input"])) == case["output"]
