"""Connecting the extension to Foray (#22): the options page, where the address and the token
go in, and the worker that has to pick a fill up again after Chrome has stopped it."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

from tests.extension.conftest import Open
from tests.extension.support import NOTHING_WRONG, TOKEN, Browser, FakeForay
from tests.extension.test_greenhouse import (
    CONFIRMATION,
    FORM,
    ID,
    PAGES,
    choose_country,
    greenhouse,
)

pytestmark = pytest.mark.slow


def options(browser: Browser) -> Page:
    page = browser.context.new_page()
    page.goto(f"chrome-extension://{browser.extension_id}/options/options.html")
    return page


def saved(browser: Browser) -> dict[str, str] | None:
    return browser.worker.evaluate("chrome.storage.local.get('connection')").get("connection")


def test_the_options_page_saves_the_connection_and_never_shows_the_token_again(
    open_browser: Open, foray: FakeForay
) -> None:
    foray.add(greenhouse())
    browser = open_browser(PAGES, connected=False)
    page = options(browser)
    port = foray.origin.rsplit(":", 1)[1]

    # Plain language about what it does and what it never does is on the page.
    words = page.inner_text("main")
    assert "You read the form and press submit." in words
    assert "It never solves or works around a CAPTCHA." in words
    assert "It never signs in and never creates an account." in words

    # Nothing is saved until it could work: an address that is none, no token, a wrong token.
    page.fill("#address", "not an address")
    page.click("#save")
    page.wait_for_selector("#status.error")
    assert "That does not read as an address." in page.inner_text("#status")
    page.fill("#address", f"127.0.0.1:{port}")
    page.click("#save")
    assert page.inner_text("#status") == "Paste a token from Foray's Settings."
    page.fill("#token", "something-else")
    page.click("#save")
    assert "That does not look like a Foray token." in page.inner_text("#status")
    assert saved(browser) is None and foray.calls == []

    page.fill("#token", "foray_ext_a-token-foray-does-not-know")
    page.click("#save")
    page.wait_for_selector("#status.error")
    assert page.inner_text("#status") == (
        "Foray did not accept this token. Make a new one in Foray's Settings."
    )

    page.fill("#token", TOKEN)
    page.click("#save")
    page.wait_for_selector("#status.ok")
    assert page.inner_text("#status") == "Connected as Avery Quinn. 1 application is waiting."
    assert saved(browser) == {"origin": foray.origin, "token": TOKEN}
    # The address is shown as it was understood; the token is gone from the page for good.
    assert page.input_value("#address") == foray.origin and page.input_value("#token") == ""
    assert TOKEN not in page.content()
    page.reload()
    page.wait_for_selector("#status.ok")
    assert page.input_value("#token") == "" and TOKEN not in page.content()
    assert page.get_attribute("#token", "placeholder") == (
        "A token is saved. Paste a new one to replace it."
    )

    # Connected, the extension works on a board's page.
    form = browser.context.new_page()
    form.goto(FORM)
    browser.wait_for_panel(form, "Fill the form")

    page.bring_to_front()
    page.click("#forget")
    page.wait_for_function(
        "document.getElementById('status').textContent.startsWith('Disconnected')"
    )
    assert saved(browser) is None


def test_the_folder_loads_as_it_ships_and_reaches_nothing_until_it_is_allowed(
    open_browser: Open, foray: FakeForay
) -> None:
    """`extension/` loaded unpacked, exactly as it is in the repository. Chrome holds nothing
    against it (checked as every test here ends), and until the user allows their Foray's
    address from the options page it cannot make a request at all."""
    foray.add(greenhouse())
    browser = open_browser(PAGES, connected=False, as_shipped=True)
    assert browser.chrome_says() == NOTHING_WRONG
    assert "It never solves or works around a CAPTCHA." in options(browser).inner_text("main")

    page = browser.context.new_page()
    page.goto(FORM)
    browser.pump(1500)
    assert browser.panel(page) == ""
    browser.popup(page).wait_for("Not connected to Foray yet.")

    # An address and a token saved without Chrome's permission for the address (which only
    # the user can give, at Chrome's own prompt): still nothing leaves the browser.
    browser.connect()
    page.reload()
    browser.pump(1500)
    assert browser.panel(page) == ""
    popup = browser.popup(page)
    popup.wait_for(f"The extension is not allowed to reach {foray.origin}.")
    assert foray.calls == []


def test_a_fill_is_remembered_when_chrome_stops_the_worker(
    open_browser: Open, foray: FakeForay
) -> None:
    """Chrome stops an idle service worker after half a minute. What a tab is in the middle of
    is kept outside the worker, so the confirmation is still reported, once."""
    foray.add(greenhouse())
    browser = open_browser(PAGES)
    page = browser.context.new_page()
    page.goto(FORM)
    browser.wait_for_panel(page, "Fill the form")
    browser.popup(page).press("#fill")
    browser.wait_for_panel(page, "Check everything, then press submit yourself.")
    page.fill("#question_5000002", "Because the tools are good.")
    choose_country(page)
    browser.pump(1200)  # the worker hears what was typed, a moment after the typing stops

    def stop_worker() -> None:
        cdp = browser.context.new_cdp_session(page)
        cdp.send("ServiceWorker.enable")
        cdp.send("ServiceWorker.stopAllWorkers")
        cdp.detach()
        browser.pump(500)

    stop_worker()
    page.click("button[type=submit]")
    page.wait_for_url(CONFIRMATION)
    sent = browser.wait_for_call("POST", f"/applications/{ID}/submitted")
    assert "Thank you for applying." in sent.body["text"]
    assert sent.body["screenshot"].startswith("data:image/png;base64,")
    # What the user typed after the fill is part of the record, though the worker was asleep
    # when submit was pressed.
    held = {item["key"]: item["value"] for item in sent.body["filled"]}
    assert held["question_5000002"] == "Because the tools are good."
    assert held["country"] == "United States +1"

    stop_worker()
    page.reload()
    browser.pump(2500)
    assert len(foray.sent("POST", "/submitted")) == 1
