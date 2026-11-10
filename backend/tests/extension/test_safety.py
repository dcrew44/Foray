"""What the extension must never do (#22), and what it keeps to itself: no sign-in, no account,
no fill when Foray says no, no fill a page's script asked for, no token where a page can read
it, no cookie on a request to Foray."""

from __future__ import annotations

import json

import pytest

from tests.extension.conftest import Open
from tests.extension.support import SESSION_COOKIE, TOKEN, FakeForay
from tests.extension.test_boards import (
    LEVER,
    LEVER_ANSWERS,
    LEVER_ID,
    LEVER_PAGES,
    unread,
)
from tests.extension.test_greenhouse import FORM, ID, PAGES, greenhouse

pytestmark = pytest.mark.slow

WORKDAY = (
    "https://northwind.wd5.myworkdayjobs.com/en-US/Careers/job/Portland/Platform-Engineer_R-1/apply"
)
WORKDAY_ID = "0c7b8e7e-0000-4000-8000-000000000005"
BLOCKED = (
    "You have sent 2 applications to Northwind Tools in the last 60 days, which is the limit "
    "you set. You can still apply yourself."
)


@pytest.mark.parametrize(
    ("query", "says"),
    [
        ("", "This page asks you to sign in. Foray never signs in for you."),
        ("?create=1", "This page asks you to create an account. Foray never makes accounts."),
    ],
)
def test_a_sign_in_or_an_account_to_create_is_handed_to_the_user(
    open_browser: Open, foray: FakeForay, query: str, says: str
) -> None:
    foray.add(unread(WORKDAY_ID, WORKDAY, "workday", may_submit=True))
    browser = open_browser({WORKDAY: "signin.html"})
    page = browser.context.new_page()
    page.goto(WORKDAY + query)

    browser.wait_for_panel(page, says)
    assert browser.world(page).evaluate("Foray.panel.actionBox()") is None  # nothing to press
    # Asked from the toolbar as well, it still fills nothing and types no password.
    browser.popup(page).press("#fill")
    browser.wait_for_panel(page, says)
    browser.pump(1500)
    assert not any(page.evaluate("Object.values(window.replica.state)"))
    assert page.evaluate("window.replica.submits") == 0
    assert foray.sent("POST", "/plan") == [] and foray.sent("POST", "/filled") == []


def test_an_application_foray_blocks_is_not_filled(open_browser: Open, foray: FakeForay) -> None:
    foray.add(greenhouse(blocked=BLOCKED))
    browser = open_browser(PAGES)
    page = browser.context.new_page()
    page.goto(FORM)

    words = browser.wait_for_panel(page, BLOCKED)
    assert "Fill the form" not in words
    assert browser.world(page).evaluate("Foray.panel.actionBox()") is None
    # The toolbar button says the same and fills nothing.
    browser.popup(page).press("#fill")
    browser.wait_for_panel(page, BLOCKED)
    browser.pump(1500)
    assert not any(page.evaluate("Object.values(window.replica.state).flat()"))
    assert foray.sent("POST", "/filled") == []
    assert page.evaluate("document.querySelectorAll('[data-foray-fill]').length") == 0


def test_a_page_cannot_start_a_fill_or_read_the_panel(open_browser: Open, foray: FakeForay) -> None:
    """The panel is in a closed shadow root and answers only a person's click, so a page's
    script can neither read which application this is nor have its form filled."""
    foray.add(greenhouse())
    browser = open_browser(PAGES)
    page = browser.context.new_page()
    page.goto(FORM)
    browser.wait_for_panel(page, "Fill the form")
    box = browser.world(page).evaluate("Foray.panel.actionBox()")
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    seen = page.evaluate(
        """([x, y]) => {
            const host = document.getElementById("foray-fill-panel");
            const at = document.elementFromPoint(x, y);
            // Everything a script can try: clicks it makes up, at the button and on its host.
            for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
              const init = { bubbles: true, cancelable: true, composed: true };
              Object.assign(init, { clientX: x, clientY: y });
              at.dispatchEvent(new MouseEvent(type, init));
              host.dispatchEvent(new MouseEvent(type, init));
            }
            host.click();
            return {
              shadow: host.shadowRoot,
              words: host.textContent + host.innerHTML,
              isHost: at === host,
              foray: typeof window.Foray,
              chromeRuntime: typeof globalThis.chrome?.runtime?.sendMessage,
            };
        }""",
        [x, y],
    )
    assert seen == {
        "shadow": None,
        "words": "",
        "isHost": True,
        "foray": "undefined",
        "chromeRuntime": "undefined",
    }
    browser.pump(1500)
    assert not any(page.evaluate("Object.values(window.replica.state).flat()"))
    assert foray.sent("POST", "/filled") == []
    assert "Fill the form" in browser.panel(page)


def test_the_token_never_reaches_a_page_and_no_request_carries_a_cookie(
    open_browser: Open, foray: FakeForay
) -> None:
    foray.add(greenhouse())
    foray.add(unread(LEVER_ID, f"{LEVER}/apply", "lever"))
    foray.answers = dict(LEVER_ANSWERS)
    browser = open_browser(PAGES | LEVER_PAGES)
    pages = []
    for url in (FORM, f"{LEVER}/apply"):
        page = browser.context.new_page()
        page.goto(url)
        browser.wait_for_panel(page, "Fill the form")
        browser.press_panel_button(page)
        browser.wait_for_panel(page, "Check everything, then press submit yourself.")
        pages.append(page)
    browser.wait_for_call("POST", f"/applications/{ID}/filled")
    browser.wait_for_call("POST", f"/applications/{LEVER_ID}/filled")

    for page in pages:
        # The page's own world: its DOM, its storage, its globals, every message it heard.
        seen = page.evaluate(
            """() => JSON.stringify({
                dom: document.documentElement.outerHTML,
                local: { ...localStorage },
                session: { ...sessionStorage },
                cookie: document.cookie,
                heard: window.replica.heard,
                globals: Object.keys(window).filter((key) => /foray|token|chrome/i.test(key)),
                state: window.replica.state,
            })"""
        )
        assert TOKEN not in seen and "foray_ext_" not in seen
        assert json.loads(seen)["globals"] == ["chrome"]  # Chrome's own, with no extension API
        assert page.evaluate("typeof chrome.storage") == "undefined"
        # The extension's world in the page is not told either: content scripts are shut
        # out of the extension's storage, and the worker never sends the token.
        world = browser.world(page)
        for area in ("local", "session"):
            refused = world.evaluate(
                f"chrome.storage.{area}.get(null).then(() => 'read', (error) => String(error))"
            )
            assert refused == "Error: Access to storage is not allowed from this context."
        assert TOKEN not in world.evaluate("JSON.stringify(Foray.main.state)")

    # Every request the extension made was signed with the token and carried no cookie, though
    # this browser holds a session cookie for Foray's address.
    cookies = browser.context.cookies(foray.origin)
    assert [cookie.get("name") for cookie in cookies] == [SESSION_COOKIE]
    assert len(foray.calls) >= 8
    for call in foray.calls:
        assert call.headers.get("Authorization") == f"Bearer {TOKEN}", call.path
        assert "Cookie" not in call.headers, call.path
        assert call.headers.get("Origin", f"chrome-extension://{browser.extension_id}") == (
            f"chrome-extension://{browser.extension_id}"
        )
    # And the token went nowhere but Foray: nothing else was even attempted.
    assert browser.aborted == []


def test_without_a_connection_the_extension_is_silent_and_says_so_when_asked(
    open_browser: Open, foray: FakeForay
) -> None:
    foray.add(greenhouse())
    browser = open_browser(PAGES, connected=False)
    page = browser.context.new_page()
    page.goto(FORM)
    browser.pump(1500)
    assert browser.panel(page) == "" and foray.calls == []

    popup = browser.popup(page)
    words = popup.wait_for("Not connected to Foray yet.")
    assert "Connect to Foray" in words

    # A token Foray does not know: nothing in the page, and the popup says what to do.
    browser.connect(token="foray_ext_not-a-token-foray-knows")
    page.reload()
    browser.pump(1500)
    assert browser.panel(page) == ""
    popup = browser.popup(page)
    popup.wait_for("Foray did not accept this token. Make a new one in Foray's Settings.")
    assert popup.evaluate("document.getElementById('fill').disabled") is True
    popup.press("#fill")
    browser.pump(1500)
    assert browser.panel(page) == ""
    assert not any(page.evaluate("Object.values(window.replica.state).flat()"))


def test_a_form_in_a_frame_on_an_employers_site_is_found_by_its_job(
    open_browser: Open, foray: FakeForay
) -> None:
    """Greenhouse's form embedded in an employer's own page: the frame is the board's, so the
    extension runs in it, and asks Foray about the posting the frame is for."""
    host = "https://careers.northwind-tools.test/open-roles/platform"
    embed = "https://job-boards.greenhouse.io/embed/job_app"
    frame_url = f"{embed}?for=northwind&token=4000001"
    foray.add(greenhouse())
    browser = open_browser(
        PAGES
        | {
            embed: "greenhouse.html",
            host: f"<!doctype html><title>Careers</title><h1>Platform Engineer</h1>"
            f"<iframe id='grnhse_iframe' src='{frame_url}' width='900' height='2600'></iframe>",
        }
    )
    page = browser.context.new_page()
    page.goto(f"{host}?gh_jid=4000001")
    frame = page.frame_locator("#grnhse_iframe").owner.content_frame.locator("body")
    frame.wait_for()
    inner = next(f for f in page.frames if f.url == frame_url)

    browser.wait_for_panel(inner, "Fill the form")
    asked = foray.sent("GET", "/applications")[-1].query["url"]
    assert asked == ["https://job-boards.greenhouse.io/northwind/jobs/4000001"]
    browser.press_panel_button(page, inner)
    browser.wait_for_panel(inner, "Check everything, then press submit yourself.")
    assert inner.evaluate("window.replica.state.first_name") == "Avery"
    assert page.evaluate("document.getElementById('foray-fill-panel')") is None
