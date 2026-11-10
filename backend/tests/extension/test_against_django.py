"""The extension against Foray itself (#22, #23): the real API, served by Django on a free
port, with a real token, a prepared application and the resume in storage. The other tests in
this folder use a fake Foray that speaks the same contract; this one is what says the fake and
the extension both have the contract right.

Playwright's sync API keeps an event loop running on the test's thread, and Django refuses the
database from a thread with a running loop. So the rows are made before the browser opens, and
read while it is open from a thread of their own (`from_django`)."""

from __future__ import annotations

import html
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from django.db import connections

from apps.applications import extension
from apps.applications.models import Application
from apps.core.storage import storage
from apps.matching.models import JobMatch
from tests.applications.test_prepare import _waiting
from tests.extension.support import (
    RESUME,
    RESUME_NAME,
    FakeForay,
    browser,
    playwright_chromium,
    sha256,
)
from tests.factories import make_document, make_resume, make_user

pytestmark = [pytest.mark.slow, pytest.mark.django_db(transaction=True)]

FORM = "https://job-boards.greenhouse.io/figma/jobs/6241542004"


def form_for(fields: list[dict[str, Any]]) -> str:
    """A plain page with one control per planned field, under the field's own name as its id,
    which is what a Greenhouse form does."""
    rows: list[str] = []
    for item in fields:
        key, label = html.escape(item["key"]), html.escape(item["label"])
        if item["type"] == "file":
            control = f'<input id="{key}" type="file" accept=".pdf">'
        elif item["type"] == "textarea":
            control = f'<textarea id="{key}"></textarea>'
        elif item["type"] in ("select", "multiselect"):
            options = "".join(f"<option>{html.escape(o)}</option>" for o in item["options"])
            control = f'<select id="{key}"><option value="">Select...</option>{options}</select>'
        else:
            control = f'<input id="{key}" type="text">'
        rows.append(f'<div class="field-wrapper"><label for="{key}">{label}</label>{control}</div>')
    return (
        "<!doctype html><title>Job Application</title>"
        '<link rel="stylesheet" href="/__replica/replica.css">'
        f'<main><form id="application-form">{"".join(rows)}'
        '<button type="submit">Submit application</button></form></main>'
        "<script>document.getElementById('application-form').addEventListener('submit', (e) => {"
        " e.preventDefault(); location.assign(location.pathname + '/confirmation'); });</script>"
    )


@pytest.fixture
def django(settings: Any, request: pytest.FixtureRequest) -> Any:
    """Django's test server on a free port. It also serves static files, from wherever
    `STATIC_URL` says, and Foray (an API behind Next) sets none: without one it fails on
    every request."""
    settings.STATIC_URL = "/static/"
    return request.getfixturevalue("live_server")


def from_django[T](read: Callable[[], T]) -> T:
    """Runs a database read on a thread with no event loop, and hands its connection back."""

    def run() -> T:
        try:
            return read()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(run).result()


def test_the_extension_fills_reports_and_proves_arrival_against_the_real_api(
    django: Any, settings: Any, tmp_path: Path
) -> None:
    settings.STORAGE_DIR = str(tmp_path / "storage")
    user = make_user(with_resume=True, name="Avery Quinn")
    pdf = make_document(user, kind="resume_pdf", filename=RESUME_NAME)
    storage().put(pdf.storage_path, RESUME)
    make_resume(user, rendered_document=pdf)
    application = _waiting(user)  # read from Greenhouse's own account of the Figma form
    made = extension.new_token(user.pk, "Test browser")
    assert made is not None
    planned = [a for a in application.answers if a["value"] is not None]
    assert len(planned) >= 5

    foray = FakeForay(token=made[1], origin=django.url)  # only the address and the token
    pages = {
        FORM: form_for(application.answers),
        f"{FORM}/confirmation": "greenhouse-confirmation.html",
    }
    with (
        playwright_chromium() as chromium,
        browser(chromium, foray, tmp_path, pages=pages) as session,
    ):
        page = session.context.new_page()
        page.goto(FORM)
        words = session.wait_for_panel(page, "Fill the form")
        assert "Figma" in words and "with your resume" in words
        assert "Why do you want to join Figma?" in words  # one of the questions still missing

        popup = session.popup(page)
        popup.wait_for("Connected as Avery Quinn.")
        popup.press("#fill")
        session.wait_for_panel(page, "Check everything, then press submit yourself.")

        # The page holds the plan, and the resume is the file in Foray's storage.
        for item in planned:
            if item["type"] == "file":
                continue
            held = page.evaluate(
                "(id) => { const el = document.getElementById(id);"
                " return el.tagName === 'SELECT' ? el.selectedOptions[0].text : el.value; }",
                item["key"],
            )
            assert held == item["value"], item["label"]
        attached = page.evaluate(
            """async () => {
                const file = document.getElementById("resume").files[0];
                const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
                const hex = [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0"));
                return [file.name, file.type, hex.join("")];
            }"""
        )
        assert attached == [RESUME_NAME, "application/pdf", sha256(RESUME)]

        # Foray was told what went in. Filling is not sending.
        def stored() -> Application:
            return from_django(lambda: Application.objects.for_user(user.pk).get(id=application.id))

        session.pump(1500)
        row = stored()
        assert row.status == "needs_answers" and row.filled_at is not None
        sent = {item["key"]: item["value"] for item in row.sent_answers or []}
        assert sent["first_name"] == "Avery" and sent["resume"] == RESUME_NAME
        assert row.submitted_at is None

        # The user answers a question and sends the form; the board's page confirms.
        page.fill("#question_20471324004", "Because the tools are good.")
        page.click("button[type=submit]")
        page.wait_for_url(f"{FORM}/confirmation")
        session.wait_for_panel(page, "Sent. Foray recorded it.", "a picture of it")

        row = stored()
        assert row.status == "submitted" and row.submitted_at is not None
        assert row.confirmation_url == f"{FORM}/confirmation"
        assert row.confirmation_text.startswith("Thank you for applying.")
        with storage().open(row.confirmation_image) as handle:
            assert handle.read(8) == b"\x89PNG\r\n\x1a\n"
        sent = {item["key"]: item["value"] for item in row.sent_answers or []}
        assert sent["question_20471324004"] == "Because the tools are good."
        match = from_django(lambda: JobMatch.objects.for_user(user.pk).get(id=application.match_id))
        assert match.stage == "applied"

        # A reload of the confirming page changes nothing.
        page.reload()
        session.pump(2000)
        assert stored().submitted_at == row.submitted_at
