"""The extension as a folder of files (#22): what its manifest asks Chrome for, and what its code
must never contain. No browser here, so these run with the ordinary suite."""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from apps.jobs.discovery import ATS_PATTERNS
from tests.extension.support import EXTENSION

MANIFEST = json.loads((EXTENSION / "manifest.json").read_text())
SHIPPED = [
    path
    for path in EXTENSION.rglob("*")
    if path.is_file() and "test" not in path.relative_to(EXTENSION).parts
]
# The boards Foray reads whose postings live on the board's own host (`jobs/sources/`).
BOARD_HOSTS = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "gem.com",
    "workable.com",
    "recruitee.com",
    "smartrecruiters.com",
    "icims.com",
    "myworkdayjobs.com",
    "myworkdaysite.com",
)


def test_the_manifest_asks_for_no_more_than_the_extension_needs() -> None:
    assert MANIFEST["manifest_version"] == 3
    # storage: the address and token. activeTab and scripting: the page the user clicks the
    # toolbar button on, and a picture of the confirmation there. Nothing else.
    assert MANIFEST["permissions"] == ["storage", "activeTab", "scripting"]
    # No site is reachable until the user allows their own Foray's address from the options
    # page; no page can message the extension; none of its files is exposed to a page.
    assert "host_permissions" not in MANIFEST
    assert MANIFEST["optional_host_permissions"] == ["http://*/*", "https://*/*"]
    assert "externally_connectable" not in MANIFEST
    assert "web_accessible_resources" not in MANIFEST
    assert "content_security_policy" not in MANIFEST  # Chrome's default: own scripts only
    assert MANIFEST["background"] == {"service_worker": "background.js", "type": "module"}


def test_content_scripts_run_only_on_the_boards_foray_reads() -> None:
    matches = [match for entry in MANIFEST["content_scripts"] for match in entry["matches"]]
    assert len(matches) == len(set(matches))
    for match in matches:
        parts = urlsplit(match.replace("*.", ""))
        assert parts.scheme == "https", match
        assert parts.hostname and parts.hostname.endswith(BOARD_HOSTS), match
    # Each board with an adapter here is one Foray recognises by the same host.
    for source in ("greenhouse", "lever", "ashby"):
        pattern = next(regex for name, regex in ATS_PATTERNS if name == source)
        assert any(pattern.search(match.replace("/*", "/acme")) for match in matches), source
    for entry in MANIFEST["content_scripts"]:
        # The extension's own isolated world, never the page's.
        assert entry.get("world", "ISOLATED") == "ISOLATED"
        # Other sites' frames only where the board's own form is the frame.
        if entry.get("all_frames"):
            assert all("/embed/job_app" in match for match in entry["matches"])


def test_every_file_the_manifest_names_is_there_and_the_worker_injects_the_same_scripts() -> None:
    named = {
        *MANIFEST["icons"].values(),
        *MANIFEST["action"]["default_icon"].values(),
        MANIFEST["action"]["default_popup"],
        MANIFEST["options_ui"]["page"],
        MANIFEST["background"]["service_worker"],
        *(script for entry in MANIFEST["content_scripts"] for script in entry["js"]),
    }
    for name in named:
        assert (EXTENSION / name).is_file(), name
    # The toolbar button injects the content scripts on a page that is not a board: the same
    # files, in the same order, as the manifest loads on one that is.
    worker = (EXTENSION / "background.js").read_text()
    listed = re.search(r"const CONTENT_FILES = \[(.*?)\];", worker, re.DOTALL)
    assert listed is not None
    injected = re.findall(r'"([^"]+)"', listed.group(1))
    for entry in MANIFEST["content_scripts"]:
        assert entry["js"] == injected


def test_no_remote_code_and_nothing_that_builds_code_from_text() -> None:
    """Manifest V3 forbids remote code; this also keeps out the ways of running text as code
    and of writing a page's markup from strings."""
    banned = re.compile(
        r"\beval\s*\(|new Function\b|importScripts\s*\(|document\.write|\.innerHTML|"
        r"insertAdjacentHTML|<script[^>]+src=[\"']https?:|@import\s+url\(\s*[\"']?https?:"
    )
    scanned = 0
    for path in SHIPPED:
        if path.suffix not in (".js", ".html", ".css"):
            continue
        scanned += 1
        found = banned.search(path.read_text())
        assert found is None, f"{path.relative_to(EXTENSION)}: {found.group(0) if found else ''}"
    assert scanned >= 15
    # Every script tag is one of the extension's own modules.
    for path in SHIPPED:
        if path.suffix == ".html":
            for tag in re.findall(r"<script[^>]*>", path.read_text()):
                assert 'type="module"' in tag and re.search(r'src="[\w./-]+\.js"', tag), tag


def test_only_the_worker_reads_the_token() -> None:
    """The token is written by the options page and read by `lib/api.js`, which only the
    service worker calls. No content script mentions storage or a request to Foray."""
    for path in (EXTENSION / "content").glob("*.js"):
        text = path.read_text()
        assert "chrome.storage" not in text and "fetch(" not in text, path.name
        assert "Authorization" not in text and "connection" not in text.lower(), path.name
    popup = (EXTENSION / "popup" / "popup.js").read_text()
    assert "chrome.storage" not in popup and "fetch(" not in popup
    readers = [
        path.relative_to(EXTENSION).as_posix()
        for path in SHIPPED
        if path.suffix == ".js" and "chrome.storage.local.get" in path.read_text()
    ]
    assert sorted(readers) == ["lib/api.js", "options/options.js"]
