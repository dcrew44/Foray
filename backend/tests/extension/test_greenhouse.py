"""The extension on a Greenhouse form (#22): a form Foray read ahead, so every answer is
planned under the field's name, which is the control's id on the page."""

from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import Page

from tests.extension.conftest import Open
from tests.extension.support import (
    RESUME,
    RESUME_NAME,
    FakeForay,
    application,
    is_png,
    planned,
    sha256,
)

pytestmark = pytest.mark.slow

FORM = "https://job-boards.greenhouse.io/northwind/jobs/4000001"
CONFIRMATION = f"{FORM}/confirmation"
ID = "0c7b8e7e-0000-4000-8000-000000000001"
PAGES = {FORM: "greenhouse.html", CONFIRMATION: "greenhouse-confirmation.html"}
WHY = "Why do you want to work at Northwind?"
AGREE = "I agree to Northwind keeping my data as its privacy notice describes"
SPONSOR = ["Yes, I will need sponsorship", "No, I will not need sponsorship"]


def greenhouse(**changes: Any) -> dict[str, Any]:
    fields = [
        planned("first_name", "First Name", "Avery", required=True, source="profile"),
        planned("last_name", "Last Name", "Quinn", required=True, source="profile"),
        planned("email", "Email", "avery.quinn@example.test", required=True, source="profile"),
        planned("phone", "Phone", "+1 207 555 0100", required=True, source="profile"),
        planned("resume", "Resume/CV", "resume", type="file", required=True, source="file"),
        planned(
            "location",
            "Location (City)",
            "Portland, ME",
            required=True,
            kind="location",
            source="profile",
        ),
        planned("question_5000001", "LinkedIn Profile", "https://linkedin.example.test/in/avery"),
        # Nothing saved for this one: it must be left exactly as it is found.
        planned("question_5000002", WHY, None, type="textarea", required=True),
        planned(
            "question_5000003",
            "Are you authorized to work in the country this job is in?",
            "Yes",
            type="select",
            required=True,
            options=["Yes", "No"],
        ),
        planned(
            "question_5000004",
            "Will you now or in the future need sponsorship to work here?",
            SPONSOR[1],
            type="select",
            required=True,
            options=SPONSOR,
        ),
        # The board listed Berlin when Foray read the form; the page no longer offers it.
        planned(
            "question_5000005",
            "Which office would you work from?",
            "Berlin",
            type="select",
            options=["Berlin", "Lisbon"],
        ),
        planned(
            "question_5000006[]",
            "Which of these have you shipped with?",
            "Go",
            type="multiselect",
            options=["Python", "Go", "Rust"],
        ),
        planned(
            "gender",
            "Gender",
            "Decline To Self Identify",
            type="select",
            options=["Male", "Female", "Decline To Self Identify"],
            source="declined",
        ),
        # Even a plan that says to tick an agreement does not get it ticked.
        planned(
            "gdpr_demographic_data_consent_given",
            AGREE,
            AGREE,
            type="multiselect",
            options=[AGREE],
        ),
    ]
    return (
        application(ID, fields, apply_url=FORM, form_source="greenhouse", form_read=True) | changes
    )


def test_a_greenhouse_form_is_filled_from_the_plan_and_nothing_is_sent(
    open_browser: Open, foray: FakeForay
) -> None:
    foray.add(greenhouse())
    browser = open_browser(PAGES)
    page = browser.context.new_page()
    page.goto(FORM)

    words = browser.wait_for_panel(page, "Fill the form")
    assert "Platform Engineer" in words and "Northwind Tools" in words
    assert "13 answers ready, with your resume." in words
    assert "1 question still needs your answer" in words and WHY in words
    assert "Nothing is sent. You read the form and press submit." in words
    # Reading the page touched nothing.
    assert not any(page.evaluate("Object.values(window.replica.state).flat()"))

    browser.press_panel_button(page)
    words = browser.wait_for_panel(page, "Check everything, then press submit yourself.")

    state = page.evaluate("window.replica.state")
    assert state["first_name"] == "Avery" and state["last_name"] == "Quinn"
    assert state["email"] == "avery.quinn@example.test"
    assert state["phone"] == "+1 207 555 0100"
    assert state["question_5000001"] == "https://linkedin.example.test/in/avery"
    # A location is searched for and chosen from what the page suggests: Maine, not Oregon.
    assert state["candidate-location"] == "Portland, Maine, United States"
    assert state["question_5000003"] == "Yes"
    assert state["question_5000004"] == SPONSOR[1]
    assert state["gender"] == "Decline To Self Identify"
    assert state["checked"] == ["question_5000006[]_2"]  # Go, and not the agreement

    # The resume is the file Foray holds, under its own name.
    assert page.evaluate("window.replica.files.resume") == {
        "name": RESUME_NAME,
        "size": len(RESUME),
        "type": "application/pdf",
        "sha256": sha256(RESUME),
    }

    # No answer, so untouched: the question nothing was saved for, and the country nobody
    # planned. An option the page does not offer is given up on, with nothing left typed.
    assert state[WHY_KEY] == "" and state["country"] == "" and state["question_5000005"] == ""
    assert page.evaluate("document.getElementById('question_5000005').value") == ""
    assert page.evaluate("document.querySelectorAll('[role=listbox]:not([hidden] *)').length") == 0

    assert "Filled 11 of 13 answers." in words
    assert "Which office would you work from? No option on the page says “Berlin”." in words
    assert "Foray never ticks agreement boxes: 1 box left for you to read." in words
    assert "Still empty and required" in words and "Country" in words and WHY in words

    # What was filled is outlined, what was not is flagged, both in plain sight.
    assert page.evaluate("document.querySelectorAll('[data-foray-fill=filled]').length") == 11
    assert page.evaluate("document.querySelectorAll('[data-foray-fill=check]').length") == 1

    # Foray is told what went in, and only that.
    told = browser.wait_for_call("POST", f"/applications/{ID}/filled").body["filled"]
    assert {item["key"]: item["value"] for item in told} == {
        "first_name": "Avery",
        "last_name": "Quinn",
        "email": "avery.quinn@example.test",
        "phone": "+1 207 555 0100",
        "resume": RESUME_NAME,
        "location": "Portland, Maine, United States",
        "question_5000001": "https://linkedin.example.test/in/avery",
        "question_5000003": "Yes",
        "question_5000004": SPONSOR[1],
        "question_5000006[]": "Go",
        "gender": "Decline To Self Identify",
    }

    # Nothing pressed submit, and nothing reported an application sent.
    assert page.evaluate("window.replica.submits") == 0
    assert page.url == FORM
    assert foray.sent("POST", "/submitted") == []
    # The corner badge of an invisible reCAPTCHA is not a hand-off.
    assert "CAPTCHA" not in words
    assert browser.aborted == []


WHY_KEY = "question_5000002"


def choose_country(page: Page) -> None:
    """The one required question Foray had no answer for, answered by the user: the form
    will not submit without it."""
    page.click("#country")
    page.get_by_role("option", name="United States +1").click()


def test_the_confirmation_is_reported_once_with_a_picture_when_started_from_the_toolbar(
    open_browser: Open, foray: FakeForay
) -> None:
    foray.add(greenhouse())
    browser = open_browser(PAGES)
    page = browser.context.new_page()
    page.goto(FORM)
    browser.wait_for_panel(page, "Fill the form")

    # The user clicks Foray in the toolbar, which is what lets Chrome take a picture later.
    browser.click_toolbar(page)
    browser.press_panel_button(page)
    words = browser.wait_for_panel(page, "Check everything, then press submit yourself.")
    assert "For a picture of it too" not in words

    # The user changes an answer, answers what was missing, and presses submit themselves.
    page.fill("#question_5000002", "Because the tools are good.")
    page.fill("#first_name", "Avery J.")
    choose_country(page)
    page.click("button[type=submit]")
    page.wait_for_url(CONFIRMATION)

    sent = browser.wait_for_call("POST", f"/applications/{ID}/submitted")
    assert sent.body["url"] == CONFIRMATION
    assert "Thank you for applying." in sent.body["text"]
    assert is_png(sent.body["screenshot"])
    assert "auto" not in sent.body  # the user pressed submit, not the extension
    # What the form held when it was sent, not only what Foray put in.
    held = {item["key"]: item["value"] for item in sent.body["filled"]}
    assert held["first_name"] == "Avery J."
    assert held["question_5000002"] == "Because the tools are good."
    assert held["question_5000003"] == "Yes" and held["resume"] == RESUME_NAME

    words = browser.wait_for_panel(page, "Sent. Foray recorded it.")
    assert "a picture of it" in words

    # A reload of the confirming page, and time passing, report nothing more.
    page.reload()
    page.wait_for_timeout(2500)
    assert len(foray.sent("POST", "/submitted")) == 1
    assert page.evaluate("window.replica.submits") == 1


def test_started_from_the_panel_the_confirmation_is_reported_without_a_picture(
    open_browser: Open, foray: FakeForay
) -> None:
    foray.add(greenhouse())
    browser = open_browser(PAGES)
    page = browser.context.new_page()
    page.goto(FORM)
    browser.wait_for_panel(page, "Fill the form")
    browser.press_panel_button(page)
    words = browser.wait_for_panel(page, "Check everything, then press submit yourself.")
    assert "For a picture of it too, click Foray in the toolbar" in words

    choose_country(page)
    page.click("button[type=submit]")
    page.wait_for_url(CONFIRMATION)
    sent = browser.wait_for_call("POST", f"/applications/{ID}/submitted")
    assert sent.body["screenshot"] == "" and "Thank you for applying." in sent.body["text"]
    words = browser.wait_for_panel(page, "Sent. Foray recorded it.")
    assert "There is no picture" in words


def test_a_confirming_page_is_not_reported_unless_submit_was_pressed_after_a_fill(
    open_browser: Open, foray: FakeForay
) -> None:
    foray.add(greenhouse())
    browser = open_browser(PAGES)
    page = browser.context.new_page()

    # Never filled: the confirming page means nothing to Foray.
    page.goto(CONFIRMATION)
    page.wait_for_timeout(2500)
    assert foray.sent("POST", "/submitted") == []

    # Filled, but the user went to the confirming address without pressing submit.
    page.goto(FORM)
    browser.wait_for_panel(page, "Fill the form")
    browser.press_panel_button(page)
    browser.wait_for_panel(page, "Check everything")
    page.goto(CONFIRMATION)
    page.wait_for_timeout(2500)
    assert foray.sent("POST", "/submitted") == []
    assert page.evaluate("window.replica.submits ?? 0") == 0
