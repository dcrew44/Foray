"""The extension on forms Foray could not read ahead (#22): Lever's plain HTML form, Ashby's
script-drawn page with no `<form>`, and an employer's own form read by its labels. Each is read
from the page, planned by Foray (`POST …/plan`), and filled from the answer."""

from __future__ import annotations

from typing import Any

import pytest

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

FILE = {
    "name": RESUME_NAME,
    "size": len(RESUME),
    "type": "application/pdf",
    "sha256": sha256(RESUME),
}


def unread(id: str, apply_url: str, source: str, **changes: Any) -> dict[str, Any]:
    """An application as Foray holds it before the page is read: only the questions every
    form asks (`forms.STANDARD_FIELDS`)."""
    fields = [
        planned("first_name", "First name", "Avery", required=True, source="profile"),
        planned("last_name", "Last name", "Quinn", required=True, source="profile"),
        planned("email", "Email", "avery.quinn@example.test", required=True, source="profile"),
        planned("phone", "Phone", "+1 207 555 0100", source="profile"),
        planned("resume", "Resume", "resume", type="file", required=True, source="file"),
    ]
    return application(id, fields, apply_url=apply_url, form_source=source, **changes)


def by_key(fields: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["key"]: item for item in fields}


# --- Lever ---------------------------------------------------------------------------------------

LEVER = "https://jobs.lever.co/northwind/11111111-2222-4333-8444-555555555555"
LEVER_ID = "0c7b8e7e-0000-4000-8000-000000000002"
LEVER_PAGES = {
    f"{LEVER}/apply": "lever.html",
    f"{LEVER}/thanks": "lever-thanks.html",
    LEVER: "<h1>Platform Engineer</h1>",
}
CARD = "cards[c0ffee00-0000-4000-8000-000000000000]"
SURVEY = "surveysResponses[5eed0000-0000-4000-8000-000000000000][responses][field0]"
YEARS = "How many years have you run production systems?"
LEVER_ANSWERS = {
    "Full name": "Avery Quinn",
    "Email": "avery.quinn@example.test",
    "Phone": "+1 207 555 0100",
    "Current location": "Portland, ME",
    "Current company": "Initech",
    "LinkedIn URL": "https://linkedin.example.test/in/avery",
    "Are you allowed to work where this job is?": "Yes",
    "Where did you hear about this job?": "A job board",
    "What would you build first?": "A deploy pipeline people trust.",
    "Which of these describe you?": "Prefer not to say",
}


def test_a_lever_form_is_read_planned_and_filled(open_browser: Open, foray: FakeForay) -> None:
    foray.add(unread(LEVER_ID, f"{LEVER}/apply", "lever"), also=[LEVER])
    foray.answers = dict(LEVER_ANSWERS)
    browser = open_browser(LEVER_PAGES)
    page = browser.context.new_page()

    # The posting itself has no form: the panel says where to go, and touches nothing.
    page.goto(LEVER)
    browser.wait_for_panel(page, "Open its form on this page and it can be filled.")
    assert foray.sent("POST", "/plan") == []

    page.goto(f"{LEVER}/apply")
    words = browser.wait_for_panel(page, "Fill the form")
    assert "11 answers ready, with your resume." in words
    assert "1 question still needs your answer" in words and YEARS in words

    # What the page was read as: every question once, by its name, with its own options, and
    # neither the marketing box nor "Apply with LinkedIn".
    read = browser.wait_for_call("POST", f"/applications/{LEVER_ID}/plan").body["fields"]
    assert [item["key"] for item in read] == [
        "resume",
        "name",
        "email",
        "phone",
        "location",
        "org",
        "urls[LinkedIn]",
        f"{CARD}[field0]",
        f"{CARD}[field1]",
        f"{CARD}[field2]",
        f"{CARD}[field3]",
        SURVEY,
    ]
    fields = by_key(read)
    assert fields["resume"] == {
        "key": "resume",
        "label": "Resume/CV",
        "type": "file",
        "required": True,
        "options": [],
    }
    assert fields["name"]["label"] == "Full name" and fields["name"]["required"] is True
    assert fields["org"]["required"] is False
    assert fields[f"{CARD}[field0]"] == {
        "key": f"{CARD}[field0]",
        "label": "Are you allowed to work where this job is?",
        "type": "select",
        "required": True,
        "options": ["Yes", "No"],
    }
    assert fields[f"{CARD}[field1]"]["options"] == [
        "A friend",
        "A job board",
        "Northwind's own site",
    ]
    assert fields[f"{CARD}[field2]"]["type"] == "textarea"
    assert fields[SURVEY]["type"] == "multiselect"
    assert fields[SURVEY]["options"] == ["Early riser", "Night owl", "Prefer not to say"]
    assert not any("consent" in item["key"] for item in read)
    untouched = {
        f"{CARD}[baseTemplate]": "{}",
        "accountId": "replica",
        "consent[marketing]": "0",
    }
    assert {k: v for k, v in page.evaluate("window.replica.state").items() if v} == untouched

    browser.press_panel_button(page)
    words = browser.wait_for_panel(page, "Check everything, then press submit yourself.")
    assert "Filled 11 of 11 answers." in words
    assert "Still empty and required" in words and YEARS in words

    state = page.evaluate("window.replica.state")
    assert state["name"] == "Avery Quinn" and state["email"] == "avery.quinn@example.test"
    assert state["phone"] == "+1 207 555 0100" and state["location"] == "Portland, ME"
    assert state["org"] == "Initech"
    assert state["urls[LinkedIn]"] == "https://linkedin.example.test/in/avery"
    assert state[f"{CARD}[field0]"] == "Yes"
    assert state[f"{CARD}[field1]"] == "A job board"
    assert state[f"{CARD}[field2]"] == "A deploy pipeline people trust."
    assert state[f"{CARD}[field3]"] == ""  # no answer: untouched
    assert state[SURVEY] == "Prefer not to say"
    assert state["consent[marketing]"] == "0"  # the box is not ticked
    assert state["h-captcha-response"] == ""
    assert page.evaluate("window.replica.files.resume") == FILE

    told = browser.wait_for_call("POST", f"/applications/{LEVER_ID}/filled").body["filled"]
    assert len(told) == 11 and {"key": "resume", "label": "Resume/CV", "value": RESUME_NAME} in told
    assert page.evaluate("window.replica.submits") == 0
    assert "CAPTCHA" not in words  # the hidden frames every Lever form loads ask for nothing

    # The user answers the last question and submits; Lever's own page confirms.
    page.fill(f"input[name='{CARD}[field3]']", "Six")
    page.click("#btn-submit")
    page.wait_for_url(f"{LEVER}/thanks")
    sent = browser.wait_for_call("POST", f"/applications/{LEVER_ID}/submitted")
    assert sent.body["url"] == f"{LEVER}/thanks"
    assert "Application submitted!" in sent.body["text"]
    held = {item["key"]: item["value"] for item in sent.body["filled"]}
    assert held[f"{CARD}[field3]"] == "Six" and held["name"] == "Avery Quinn"
    browser.pump(2500)
    assert len(foray.sent("POST", "/submitted")) == 1


def test_submit_is_pressed_only_when_foray_allows_it_and_the_page_asks_for_nobody(
    open_browser: Open, foray: FakeForay
) -> None:
    """`maySubmit` (#40). Allowed and nothing in the way: the extension presses submit and
    says so. Allowed, but the page shows a CAPTCHA: it stops with the form filled."""
    foray.add(unread(LEVER_ID, f"{LEVER}/apply", "lever", may_submit=True))
    foray.answers = LEVER_ANSWERS | {YEARS: "Six"}
    browser = open_browser(LEVER_PAGES)

    # A CAPTCHA on screen: filled, handed off, not sent.
    page = browser.context.new_page()
    page.goto(f"{LEVER}/apply?captcha=1")
    words = browser.wait_for_panel(page, "Fill the form")
    assert "You let Foray send this one itself." in words
    browser.press_panel_button(page)
    words = browser.wait_for_panel(page, "This page is asking for a CAPTCHA.")
    assert "Foray never solves those: complete it yourself, then press submit." in words
    assert "Filled 12 of 12 answers." in words and "Foray pressed submit" not in words
    browser.pump(1500)
    assert page.evaluate("window.replica.submits") == 0 and page.url.endswith("/apply?captcha=1")
    assert page.evaluate("window.replica.state")["name"] == "Avery Quinn"
    assert foray.sent("POST", "/submitted") == []
    page.close()

    # Nothing in the way: the extension presses submit, and says the press was its own.
    page = browser.context.new_page()
    page.goto(f"{LEVER}/apply")
    browser.wait_for_panel(page, "Fill the form")
    browser.popup(page).press("#fill")  # from the toolbar, so the confirmation has a picture
    page.wait_for_url(f"{LEVER}/thanks")
    assert page.evaluate("window.replica.submits") == 1
    sent = browser.wait_for_call("POST", f"/applications/{LEVER_ID}/submitted")
    assert sent.body["auto"] is True
    assert is_png(sent.body["screenshot"])
    assert "Application submitted!" in sent.body["text"]


def test_a_challenge_that_appears_on_submit_is_left_to_the_user(
    open_browser: Open, foray: FakeForay
) -> None:
    """Allowed to submit and nothing in the way, the extension presses submit, and the board
    answers with a CAPTCHA. That is where it stops: one press, the challenge untouched,
    nothing reported."""
    foray.add(unread(LEVER_ID, f"{LEVER}/apply", "lever", may_submit=True))
    foray.answers = LEVER_ANSWERS | {YEARS: "Six"}
    browser = open_browser(LEVER_PAGES)
    page = browser.context.new_page()
    page.goto(f"{LEVER}/apply?captcha=late")
    browser.wait_for_panel(page, "Fill the form")
    browser.press_panel_button(page)
    words = browser.wait_for_panel(page, "This page is asking for a CAPTCHA.")
    assert "Foray pressed submit, as you allowed for this application." in words
    browser.pump(3000)
    assert page.evaluate("[window.replica.presses, window.replica.submits]") == [1, 0]
    assert page.evaluate("window.replica.state['h-captcha-response']") == ""
    assert page.url.endswith("/apply?captcha=late")
    assert foray.sent("POST", "/submitted") == []


def test_submit_is_not_pressed_while_anything_is_unanswered_even_when_allowed(
    open_browser: Open, foray: FakeForay
) -> None:
    foray.add(unread(LEVER_ID, f"{LEVER}/apply", "lever", may_submit=True))
    foray.answers = dict(LEVER_ANSWERS)  # nothing saved for the years question
    browser = open_browser(LEVER_PAGES)
    page = browser.context.new_page()
    page.goto(f"{LEVER}/apply")
    browser.wait_for_panel(page, "Fill the form")
    browser.press_panel_button(page)
    words = browser.wait_for_panel(page, "Check everything, then press submit yourself.")
    assert "Foray pressed submit" not in words
    browser.pump(1500)
    assert page.evaluate("window.replica.submits") == 0


# --- Ashby ---------------------------------------------------------------------------------------

ASHBY = "https://jobs.ashbyhq.com/northwind/aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
ASHBY_ID = "0c7b8e7e-0000-4000-8000-000000000003"
ASHBY_PAGES = {ASHBY: "ashby.html", f"{ASHBY}/application": "ashby.html"}
Q = "9d1c0000-0000-4000-8000-00000000000"
ASHBY_ANSWERS = {
    "Full Name": "Avery Quinn",
    "Email": "avery.quinn@example.test",
    "Phone Number": "+1 207 555 0100",
    "Current Location": "Portland, ME",
    "Are you allowed to work where this job is?": "Yes",
    "Have you worked at Northwind before?": "No",
    "Which teams interest you?": "Platform",
    "How did you hear about this job?": "A job board",
    "How do you describe your gender?": "Prefer not to say",
}
BUILD = "What would you build first?"


def test_an_ashby_form_is_found_when_it_is_drawn_and_filled_by_its_labels(
    open_browser: Open, foray: FakeForay
) -> None:
    foray.add(unread(ASHBY_ID, f"{ASHBY}/application", "ashby"), also=[ASHBY])
    foray.answers = dict(ASHBY_ANSWERS)
    browser = open_browser(ASHBY_PAGES)
    page = browser.context.new_page()

    # The posting's overview: no form yet. Its Application tab draws one without a page load.
    page.goto(ASHBY)
    browser.wait_for_panel(page, "Open its form on this page and it can be filled.")
    page.click("#job-application-form")
    words = browser.wait_for_panel(page, "Fill the form")
    assert "10 answers ready, with your resume." in words
    assert "1 question still needs your answer" in words and BUILD in words

    read = browser.wait_for_call("POST", f"/applications/{ASHBY_ID}/plan").body["fields"]
    assert [(item["key"], item["label"], item["type"], item["required"]) for item in read] == [
        ("_systemfield_resume", "Resume", "file", True),
        ("_systemfield_name", "Full Name", "text", True),
        ("_systemfield_email", "Email", "text", True),
        (f"{Q}1", "Phone Number", "text", False),
        ("_systemfield_location", "Current Location", "select", True),
        (f"{Q}2", "Are you allowed to work where this job is?", "select", True),
        (f"{Q}3", "Have you worked at Northwind before?", "select", True),
        (f"{Q}4", "Which teams interest you?", "multiselect", False),
        (f"{Q}5", "How did you hear about this job?", "select", True),
        (f"{Q}6", BUILD, "textarea", True),
        (f"{Q}7", "How do you describe your gender?", "select", False),
    ]
    fields = by_key(read)
    assert fields[f"{Q}2"]["options"] == ["Yes", "No"]  # the Yes / No buttons
    assert fields[f"{Q}4"]["options"] == ["Platform", "Data", "Mobile"]
    # A list that only opens is not opened until the user asks for the fill.
    assert fields[f"{Q}5"]["options"] == []
    assert page.evaluate("document.querySelectorAll('[role=listbox]').length") == 0

    browser.press_panel_button(page)
    words = browser.wait_for_panel(page, "Check everything, then press submit yourself.")
    assert "Filled 10 of 10 answers." in words
    assert "Foray never ticks agreement boxes: 1 box left for you to read." in words

    # At the fill the list was opened once to read its options, and planned again with them.
    again = by_key(foray.sent("POST", "/plan")[-1].body["fields"])
    assert again[f"{Q}5"]["options"] == ["A friend", "A job board", "Northwind's own site"]

    state = page.evaluate("window.replica.state")
    assert state["_systemfield_name"] == "Avery Quinn"  # a framework-owned input took it
    assert state["_systemfield_email"] == "avery.quinn@example.test"
    assert state[f"{Q}1"] == "+1 207 555 0100"
    assert state["_systemfield_location"] == "Portland, Maine, United States"
    assert state[f"{Q}2"] == "Yes" and state[f"{Q}3"] == "No"
    assert state[f"{Q}4"] == ["Platform"]
    assert state[f"{Q}5"] == "A job board"
    assert state[f"{Q}6"] == ""  # nothing saved: untouched
    assert state[f"{Q}7"] == ["Prefer not to say"]
    assert state["_systemfield_data_consent_ack"] == []  # "I agree" is the user's to tick
    # The resume went into the form's own field, not into "Autofill from resume".
    assert page.evaluate("window.replica.files") == {"_systemfield_resume": FILE}
    assert page.evaluate("window.replica.submits") == 0

    # The page confirms in place, with no navigation.
    page.fill(f"[id='{Q}6']", "A deploy pipeline people trust.")
    page.click(".ashby-application-form-submit-button")
    sent = browser.wait_for_call("POST", f"/applications/{ASHBY_ID}/submitted")
    assert sent.body["url"] == f"{ASHBY}/application"
    assert "Your application was successfully submitted." in sent.body["text"]
    held = {item["key"]: item["value"] for item in sent.body["filled"]}
    assert held[f"{Q}6"] == "A deploy pipeline people trust." and held[f"{Q}2"] == "Yes"
    browser.wait_for_panel(page, "Sent. Foray recorded it.")
    browser.pump(2500)
    assert len(foray.sent("POST", "/submitted")) == 1


# --- an employer's own form ----------------------------------------------------------------------

OWN = "https://careers.northwind-tools.test/jobs/platform-engineer/apply"
OWN_ID = "0c7b8e7e-0000-4000-8000-000000000004"
OWN_ANSWERS = {
    "First name": "Avery",
    "Last name": "Quinn",
    "Email address": "avery.quinn@example.test",
    "Phone number": "+1 207 555 0100",
    "Where are you based?": "Portland, ME",
    "How much notice do you need to give?": "Two weeks",
    "Do you need a visa to work here?": "No",
    "Which days could you be in the office?": "Wednesday",
    "Why this job?": "Because the tools are good.",
}


def test_any_other_form_is_filled_from_the_toolbar_by_its_labels(
    open_browser: Open, foray: FakeForay
) -> None:
    foray.add(unread(OWN_ID, OWN, ""))
    foray.answers = dict(OWN_ANSWERS)
    browser = open_browser({OWN: "generic.html"})
    page = browser.context.new_page()
    page.goto(OWN)

    # Not one of the boards: the extension is not on this page, and asks Foray nothing.
    browser.pump(1500)
    assert page.evaluate("document.getElementById('foray-fill-panel')") is None
    assert foray.sent("GET", "/applications") == []

    # The user clicks Foray in the toolbar, then the popup's one button.
    popup = browser.popup(page)
    words = popup.wait_for("Connected as Avery Quinn.")
    assert "1 application is waiting in Foray" in words
    popup.press("#fill")
    words = browser.wait_for_panel(page, "Check everything, then press submit yourself.")
    assert "Filled 10 of 10 answers." in words
    assert "Foray never ticks agreement boxes: 2 boxes left for you to read." in words
    assert not popup.is_open()  # it closed, and gave the page its focus back

    # The plan round trip: what the page was read as, each question by its own kind of label.
    read = foray.sent("POST", "/plan")[-1].body["fields"]
    assert [(item["key"], item["label"], item["type"], item["required"]) for item in read] == [
        ("applicant-first", "First name", "text", True),
        ("last", "Last name", "text", True),
        ("email", "Email address", "text", True),
        ("phone", "Phone number", "text", False),
        ("city", "Where are you based?", "text", False),
        ("cv-file", "Resume", "file", True),
        ("notice", "How much notice do you need to give?", "select", False),
        ("visa", "Do you need a visa to work here?", "select", True),
        ("days", "Which days could you be in the office?", "multiselect", False),
        ("pitch", "Why this job?", "textarea", True),
        ("salary", "What salary are you looking for?", "text", False),
    ]
    fields = by_key(read)
    assert fields["notice"]["options"] == ["None", "Two weeks", "One month"]
    assert fields["visa"]["options"] == ["Yes", "No"]
    assert fields["days"]["options"] == ["Monday", "Wednesday", "Friday"]

    assert page.evaluate("window.replica.state") == {
        "first": "Avery",
        "last": "Quinn",
        "email": "avery.quinn@example.test",
        "phone": "+1 207 555 0100",
        "city": "Portland, ME",
        "notice": "2w",
        "visa": "n",
        "days": "wed",
        "pitch": "Because the tools are good.",
        "salary": "",  # no answer: untouched
        "website": "",  # the field nobody can see: untouched
        # and neither the terms nor the newsletter box is ticked
    }
    assert page.evaluate("window.replica.files.cv") == FILE
    assert page.evaluate("window.replica.submits") == 0

    told = browser.wait_for_call("POST", f"/applications/{OWN_ID}/filled").body["filled"]
    assert {"key": "visa", "label": "Do you need a visa to work here?", "value": "No"} in told
    assert {
        "key": "notice",
        "label": "How much notice do you need to give?",
        "value": "Two weeks",
    } in told

    # The user ticks the terms themselves and sends it; the page confirms in place.
    page.check("input[name=terms]")
    page.click("button[type=submit]")
    sent = browser.wait_for_call("POST", f"/applications/{OWN_ID}/submitted")
    assert "Thanks for applying!" in sent.body["text"] and sent.body["url"] == OWN
    assert is_png(sent.body["screenshot"])
    assert "auto" not in sent.body
    browser.pump(2500)
    assert len(foray.sent("POST", "/submitted")) == 1
