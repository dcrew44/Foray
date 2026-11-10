"""What the browser extension's tests stand on (#22).

- `FakeForay`: a small local server that speaks the extension's side of Foray's API
  (`apps/applications/api.py`, the `/extension` router) and records everything it is sent. It
  signs requests in the way Django does: a bearer token, and a refusal for anything that
  carries the session cookie.
- `Browser`: a real Chromium with the extension loaded from a copy of `extension/`. The copy
  differs from the folder in one line: the fake server's origin is granted in the manifest,
  because the prompt Chrome shows for an optional permission cannot be pressed by a script.
- The replica forms (`extension/test/replicas/`) are served by the browser itself at the
  job boards' own addresses, so the manifest's real `matches` decide where the content script
  runs. No request leaves the machine: every name but loopback fails to resolve, and anything
  the routes do not serve is aborted.
- `World`: the extension's isolated world in a page, where its panel lives. The panel is in a
  closed shadow root and only answers a real click, so tests read it there and click on it
  with the mouse.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import BrowserContext, CDPSession, Frame, Page, Playwright, Route, Worker

REPO = Path(__file__).resolve().parents[3]
EXTENSION = REPO / "extension"
REPLICAS = EXTENSION / "test" / "replicas"

TOKEN = "foray_ext_test-token-made-up-for-the-replica-tests-0123456789"
RESUME = b"%PDF-1.7\n% a resume that exists only in the tests\n" + bytes(range(256)) * 8
RESUME_NAME = "Avery Quinn - Resume.pdf"
SESSION_COOKIE = "sessionid"
# What stands in for a CAPTCHA provider's frame: the replicas point at the providers' real
# addresses, because the address is how the extension knows a CAPTCHA, and get this instead.
STUB_FRAME = "<!doctype html><title>stub</title><body style='margin:0;background:#ddd'>stub</body>"
CAPTCHA_HOSTS = ("www.recaptcha.net", "newassets.hcaptcha.com", "challenges.cloudflare.com")

_GREENHOUSE_ID = re.compile(r"/jobs/(\d+)")


# --- the fake Foray ------------------------------------------------------------------------------


def planned(
    key: str,
    label: str,
    value: str | None,
    *,
    type: str = "text",
    required: bool = False,
    options: list[str] | None = None,
    kind: str = "custom",
    source: str | None = None,
) -> dict[str, Any]:
    """One planned field, as `prepare.Planned` reaches the extension."""
    if source is None:
        source = "saved" if value is not None else ("missing" if required else "skipped")
    return {
        "key": key,
        "label": label,
        "type": type,
        "required": required,
        "options": options or [],
        "kind": kind,
        "value": value,
        "source": source,
    }


def application(
    id: str,
    fields: list[dict[str, Any]],
    *,
    company: str = "Northwind Tools",
    title: str = "Platform Engineer",
    apply_url: str = "",
    form_source: str = "",
    form_read: bool = False,
    resume: bool = True,
    blocked: str | None = None,
    may_submit: bool = False,
) -> dict[str, Any]:
    """A `FillPayload`, in the wire's camelCase."""
    return {
        "id": id,
        "status": "ready",
        "company": company,
        "title": title,
        "applyUrl": apply_url,
        "formSource": form_source,
        "formRead": form_read,
        "fields": fields,
        "missing": [f["label"] for f in fields if f["source"] == "missing" and f["required"]],
        "resume": {"filename": RESUME_NAME, "sizeBytes": len(RESUME)} if resume else None,
        "blocked": blocked,
        "maySubmit": may_submit,
    }


@dataclass
class Call:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: Any


@dataclass
class FakeForay:
    """Foray, as far as the extension can tell."""

    token: str = TOKEN
    name: str = "Avery Quinn"
    applications: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Answers the planner gives for a form read from the page, by the question's label.
    answers: dict[str, str] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)
    origin: str = ""
    # Other addresses an application answers to (its posting, beside its apply link).
    also: dict[str, list[str]] = field(default_factory=dict)
    _server: ThreadingHTTPServer | None = None

    # -- what a test sets up and reads back --

    def add(self, payload: dict[str, Any], *, also: list[str] | None = None) -> dict[str, Any]:
        self.applications[payload["id"]] = payload
        self.also[payload["id"]] = also or []
        return payload

    def sent(self, method: str, suffix: str) -> list[Call]:
        return [c for c in self.calls if c.method == method and c.path.endswith(suffix)]

    # -- the API --

    def find(self, url: str) -> dict[str, Any] | None:
        """`extension.find_for_url`: the same address, or the same Greenhouse job id."""
        try:
            parts = urlsplit(url)
        except ValueError:
            return None
        job = (parse_qs(parts.query).get("gh_jid") or [None])[0]
        in_path = _GREENHOUSE_ID.search(parts.path)
        greenhouse_id = job or (in_path.group(1) if in_path else None)
        for payload in self.applications.values():
            if payload["status"] not in ("ready", "needs_answers"):
                continue
            for link in [payload["applyUrl"], *self.also.get(payload["id"], [])]:
                other = urlsplit(link)
                if other.hostname == parts.hostname and other.path.rstrip("/") == parts.path.rstrip(
                    "/"
                ):
                    return payload
            own = _GREENHOUSE_ID.search(urlsplit(payload["applyUrl"]).path)
            if (
                greenhouse_id
                and payload["formSource"] == "greenhouse"
                and own
                and own.group(1) == greenhouse_id
            ):
                return payload
        return None

    def plan(self, payload: dict[str, Any], fields: list[dict[str, Any]]) -> dict[str, Any]:
        """`extension.plan_fields`, with the planner reduced to a table of answers."""
        if payload["formRead"]:
            return payload
        out: list[dict[str, Any]] = []
        for item in fields:
            label, options = item["label"], item.get("options") or []
            kind = item.get("type", "text")
            required = bool(item.get("required"))
            if kind == "file":
                is_resume = label.lower().startswith(("resume", "cv"))
                out.append(
                    planned(
                        item["key"],
                        label,
                        "resume" if is_resume else None,
                        type="file",
                        required=required,
                        kind="resume" if is_resume else "file",
                        source="file" if is_resume else None,
                    )
                )
                continue
            answer = self.answers.get(label)
            if answer is not None and options and answer not in options:
                answer = None  # a saved answer none of the options says is missing
            out.append(
                planned(item["key"], label, answer, type=kind, required=required, options=options)
            )
        payload["fields"] = out
        payload["missing"] = [f["label"] for f in out if f["source"] == "missing" and f["required"]]
        payload["status"] = "needs_answers" if payload["missing"] else "ready"
        return payload

    def start(self) -> None:
        foray = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # quiet
                pass

            def _answer(self, status: int, body: Any, content_type: str = "application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                if content_type == "application/pdf":
                    self.send_header("Content-Disposition", f'attachment; filename="{RESUME_NAME}"')
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, method: str) -> None:
                parts = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body: Any
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = raw.decode(errors="replace")
                foray.calls.append(
                    Call(
                        method, parts.path, parse_qs(parts.query), dict(self.headers.items()), body
                    )
                )
                # Django's order: the origin check refuses a browser's request first, then the
                # router asks for the token.
                if SESSION_COOKIE in (self.headers.get("Cookie") or ""):
                    return self._answer(
                        403, {"ok": False, "message": "Cross-site request refused."}
                    )
                if self.headers.get("Authorization") != f"Bearer {foray.token}":
                    return self._answer(401, {"detail": "Unauthorized"})
                if not parts.path.startswith("/api/extension/"):
                    return self._answer(404, {"ok": False, "message": "Not found."})
                route = parts.path.removeprefix("/api/extension/").strip("/").split("/")
                if route == ["me"] and method == "GET":
                    waiting = sum(
                        1
                        for p in foray.applications.values()
                        if p["status"] in ("ready", "needs_answers")
                    )
                    return self._answer(200, {"ok": True, "name": foray.name, "waiting": waiting})
                if route == ["applications"] and method == "GET":
                    url = (parse_qs(parts.query).get("url") or [""])[0]
                    return self._answer(200, {"application": foray.find(url)})
                payload = foray.applications.get(route[1]) if len(route) > 1 else None
                if route[0] != "applications" or payload is None:
                    return self._answer(
                        404, {"ok": False, "message": "That application was not found."}
                    )
                action = route[2] if len(route) > 2 else ""
                if method == "GET" and action == "":
                    return self._answer(200, payload)
                if method == "GET" and action == "resume":
                    if payload["resume"] is None:
                        return self._answer(
                            404, {"ok": False, "message": "There is no resume PDF yet."}
                        )
                    return self._answer(200, RESUME, "application/pdf")
                if method == "POST" and action == "plan":
                    read = body.get("fields", []) if isinstance(body, dict) else []
                    return self._answer(200, foray.plan(payload, read))
                if method == "POST" and action == "filled":
                    return self._answer(200, {"ok": True, "message": "Noted"})
                if method == "POST" and action == "submitted":
                    first = payload["status"] != "submitted"
                    payload["status"] = "submitted"
                    message = (
                        "Recorded. The match moved to Applied." if first else "Already recorded"
                    )
                    return self._answer(200, {"ok": True, "message": message})
                return self._answer(404, {"ok": False, "message": "Not found."})

            def do_GET(self) -> None:
                self._handle("GET")

            def do_POST(self) -> None:
                self._handle("POST")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


# --- the browser ---------------------------------------------------------------------------------


def packaged(target: Path, *, allow: str) -> Path:
    """A copy of the extension as it ships (no tests), with one origin granted up front."""
    shutil.copytree(EXTENSION, target, ignore=shutil.ignore_patterns("test"))
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["host_permissions"] = [allow]
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return target


type Pages = dict[str, str | Callable[[str], str]]


class World:
    """The extension's isolated world in a page or frame."""

    def __init__(self, session: Browser, target: Page | Frame) -> None:
        self.session = session
        self.context = session.context
        self.target = target
        self.origin = f"chrome-extension://{session.extension_id}"

    def evaluate(self, expression: str, *, timeout: float = 10) -> Any:
        """Evaluates in the content script's world. Waits for the script to be there."""
        end = time.monotonic() + timeout
        last: Exception | None = None
        while time.monotonic() < end:
            try:
                return self._once(expression)
            except Exception as error:  # the page is still loading, or just navigated
                last = error
                self.session.pump()
        raise AssertionError(f"the extension's world never answered: {last}")

    def _once(self, expression: str) -> Any:
        cdp: CDPSession = self.context.new_cdp_session(self.target)
        try:
            contexts: list[dict[str, Any]] = []
            cdp.on(
                "Runtime.executionContextCreated", lambda event: contexts.append(event["context"])
            )
            cdp.send("Runtime.enable")
            frame_id = cdp.send("Page.getFrameTree")["frameTree"]["frame"]["id"]
            mine = [
                c
                for c in contexts
                if c.get("origin", "").startswith(self.origin)
                and c.get("auxData", {}).get("frameId") == frame_id
            ]
            if not mine:
                raise LookupError("no content script in this frame yet")
            result = cdp.send(
                "Runtime.evaluate",
                {
                    "expression": expression,
                    "contextId": mine[-1]["id"],
                    "returnByValue": True,
                    "awaitPromise": True,
                },
            )
            if "exceptionDetails" in result:
                raise RuntimeError(result["exceptionDetails"].get("text", "evaluation failed"))
            return result["result"].get("value")
        finally:
            cdp.detach()


@dataclass
class Browser:
    context: BrowserContext
    worker: Worker
    extension_id: str
    foray: FakeForay
    aborted: list[str] = field(default_factory=list)

    def world(self, target: Page | Frame) -> World:
        return World(self, target)

    def pump(self, ms: float = 100) -> None:
        """Waits without stopping the browser's requests: Playwright serves them (the
        replicas, and the extension's calls to the fake Foray pass through its routes) on
        this thread, so a plain `time.sleep` would hold them all up."""
        self.context.pages[0].wait_for_timeout(ms)

    def wait_for_call(
        self, method: str, suffix: str, *, count: int = 1, timeout: float = 15
    ) -> Call:
        """The `count`th request of this kind Foray was sent, once it has been."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            found = self.foray.sent(method, suffix)
            if len(found) >= count:
                return found[count - 1]
            self.pump(50)
        raise AssertionError(f"Foray was never sent {method} …{suffix} ({count} wanted)")

    # -- the panel --

    def panel(self, target: Page | Frame) -> str:
        return self.world(target).evaluate("globalThis.Foray?.panel?.words() ?? ''") or ""

    def wait_for_panel(self, target: Page | Frame, *needles: str, timeout: float = 15) -> str:
        end = time.monotonic() + timeout
        words = ""
        while time.monotonic() < end:
            words = self.panel(target)
            if all(needle in words for needle in needles):
                return words
            self.pump()
        raise AssertionError(f"the panel never said {needles!r}; it says {words!r}")

    def press_panel_button(self, page: Page, frame: Frame | None = None) -> None:
        """A real click on the panel's main button, as a person makes one."""
        box = self.world(frame or page).evaluate("globalThis.Foray.panel.actionBox()")
        assert box and not box["disabled"], f"the panel has no button to press: {box}"
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        if frame is not None:
            offset = frame.frame_element().bounding_box()
            assert offset is not None
            x, y = x + offset["x"], y + offset["y"]
        page.mouse.click(x, y)

    # -- the toolbar --

    def click_toolbar(self, page: Page) -> None:
        """Clicks the extension's toolbar button on this tab, the way the browser's own UI
        does: Chrome grants `activeTab` for it and opens the popup."""
        assert self.context.browser is not None
        cdp = self.context.browser.new_browser_cdp_session()
        try:
            targets = cdp.send("Target.getTargets", {"filter": [{"type": "tab"}]})["targetInfos"]
            tab = next(t for t in targets if t["url"] == page.url)
            cdp.send(
                "Extensions.triggerAction", {"id": self.extension_id, "targetId": tab["targetId"]}
            )
        finally:
            cdp.detach()

    def popup(self, page: Page) -> Popup:
        """The toolbar popup, opened on this tab by a click on the toolbar button."""
        self.click_toolbar(page)
        return Popup(self)

    def connect(self, *, origin: str | None = None, token: str | None = None) -> None:
        """Saves the connection as the options page does, without the page."""
        value = {"origin": origin or self.foray.origin, "token": token or self.foray.token}
        self.worker.evaluate("value => chrome.storage.local.set({connection: value})", value)

    def serve(self, pages: Pages) -> None:
        """Adds replica pages: an address (without its query) and the file or text to serve."""
        self._pages.update(pages)

    _pages: Pages = field(default_factory=dict)


class Popup:
    """The real toolbar popup. Playwright does not list an extension's popup among its
    pages, so this talks to it over the browser's own debugging connection."""

    def __init__(self, session: Browser) -> None:
        assert session.context.browser is not None
        self.session = session
        self.cdp = session.context.browser.new_browser_cdp_session()
        self.answers: dict[int, dict[str, Any]] = {}
        self.sent = 0
        self.cdp.on("Target.receivedMessageFromTarget", self._heard)
        url = f"chrome-extension://{session.extension_id}/popup/popup.html"
        end = time.monotonic() + 10
        target = None
        while target is None and time.monotonic() < end:
            found = self.cdp.send("Target.getTargets", {"filter": [{"type": "page"}]})
            target = next((t for t in found["targetInfos"] if t["url"] == url), None)
            if target is None:
                session.pump()
        assert target is not None, "the popup never opened"
        self.target_id = target["targetId"]
        attached = self.cdp.send(
            "Target.attachToTarget", {"targetId": self.target_id, "flatten": False}
        )
        self.session_id = attached["sessionId"]

    def _heard(self, event: dict[str, Any]) -> None:
        message = json.loads(event["message"])
        if "id" in message:
            self.answers[message["id"]] = message

    def evaluate(self, expression: str) -> Any:
        self.sent += 1
        mine = self.sent
        self.cdp.send(
            "Target.sendMessageToTarget",
            {
                "sessionId": self.session_id,
                "message": json.dumps(
                    {
                        "id": mine,
                        "method": "Runtime.evaluate",
                        "params": {
                            "expression": expression,
                            "returnByValue": True,
                            "userGesture": True,
                        },
                    }
                ),
            },
        )
        end = time.monotonic() + 10
        while mine not in self.answers and time.monotonic() < end:
            self.session.pump(50)
        return self.answers[mine]["result"]["result"].get("value")

    def words(self) -> str:
        return self.evaluate("document.body.innerText")

    def wait_for(self, needle: str, timeout: float = 10) -> str:
        end = time.monotonic() + timeout
        words = ""
        while time.monotonic() < end:
            words = self.words() or ""
            if needle in words:
                return words
            self.session.pump()
        raise AssertionError(f"the popup never said {needle!r}; it says {words!r}")

    def press(self, selector: str) -> None:
        self.evaluate(f"document.querySelector({json.dumps(selector)}).click()")

    def is_open(self) -> bool:
        found = self.cdp.send("Target.getTargets", {"filter": [{"type": "page"}]})
        return any(t["targetId"] == self.target_id for t in found["targetInfos"])


def _fulfil(route: Route, pages: Pages, aborted: list[str]) -> None:
    url = route.request.url
    parts = urlsplit(url)
    if (
        parts.scheme in ("chrome-extension", "data", "blob", "about")
        or parts.hostname == "127.0.0.1"
    ):
        return route.continue_()
    if parts.path.startswith("/__replica/"):
        name = parts.path.removeprefix("/__replica/")
        kind = "text/css" if name.endswith(".css") else "text/javascript"
        return route.fulfill(status=200, content_type=kind, body=(REPLICAS / name).read_text())
    if parts.hostname in CAPTCHA_HOSTS:
        return route.fulfill(status=200, content_type="text/html", body=STUB_FRAME)
    bare = f"{parts.scheme}://{parts.netloc}{parts.path}"
    served = pages.get(bare)
    if served is None:
        aborted.append(url)
        return route.abort()
    body = served(url) if callable(served) else served
    if body.endswith(".html") and "\n" not in body:
        body = (REPLICAS / body).read_text()
    return route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)


@contextmanager
def browser(
    playwright: Playwright,
    foray: FakeForay,
    tmp_path: Path,
    *,
    pages: Pages | None = None,
    connected: bool = True,
    headless: bool = True,
) -> Iterator[Browser]:
    """Chromium with the extension loaded and the replicas at the boards' own addresses."""
    host = urlsplit(foray.origin).hostname
    extension = packaged(tmp_path / "extension", allow=f"http://{host}/*")
    context = playwright.chromium.launch_persistent_context(
        str(tmp_path / "profile"),
        channel="chromium",  # the full browser: the headless shell cannot load extensions
        headless=headless,
        viewport={"width": 1280, "height": 900},
        args=[
            f"--disable-extensions-except={extension}",
            f"--load-extension={extension}",
            # No name resolves but this machine's: a replica can never be the real site.
            "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost",
            # Lets a test click the toolbar button (CDP's Extensions.triggerAction).
            "--enable-unsafe-extension-debugging",
        ],
    )
    try:
        served: Pages = dict(pages or {})
        aborted: list[str] = []
        context.route("**/*", lambda route: _fulfil(route, served, aborted))
        worker = (
            context.service_workers[0]
            if context.service_workers
            else context.wait_for_event("serviceworker")
        )
        # A browser tab's session with Foray: the extension's requests must never carry it.
        context.add_cookies(
            [{"name": SESSION_COOKIE, "value": "a-signed-in-browser", "url": foray.origin}]
        )
        session = Browser(context, worker, worker.url.split("/")[2], foray, aborted)
        session._pages = served
        if connected:
            session.connect()
        yield session
    finally:
        context.close()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_png(data_url: str) -> bool:
    head, _, encoded = data_url.partition(",")
    return head == "data:image/png;base64" and base64.b64decode(encoded).startswith(b"\x89PNG")
