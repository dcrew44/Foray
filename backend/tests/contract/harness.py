"""The UI-stream contract harness (design §12.4).

`validate_stream.mjs` reads SSE files with the browser's own reader from the repo's installed
`ai@7` and compares the message the client rebuilds with the one the server saved. A test writes
one case per scenario into a directory and checks them all with one Node process:

    write_case(tmp_path, "text", sse, persisted=acc.message)
    report = assert_contract(tmp_path)          # fails on any rejected chunk or mismatch
    assert report["text"]["errorTexts"] == []

Node and `node_modules/ai` are required under `CI=1`; elsewhere their absence skips the test
with a warning, so an `ai` upgrade that tightens the schema fails loudly where it is checked.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

CONTRACT = Path(__file__).resolve().parent
SCRIPT = CONTRACT / "validate_stream.mjs"
REPO_ROOT = CONTRACT.parents[2]
GOLDENS = CONTRACT.parent / "goldens" / "uistream"


def parse_sse(text: str) -> list[dict[str, Any]]:
    """The JSON chunks of an SSE body (comments, `[DONE]` and non-data fields skipped)."""
    chunks: list[dict[str, Any]] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        lines = [
            line[5:].removeprefix(" ") for line in block.split("\n") if line.startswith("data:")
        ]
        data = "\n".join(lines)
        if data and data != "[DONE]":
            chunks.append(json.loads(data))
    return chunks


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def write_case(
    directory: Path,
    name: str,
    sse: str,
    persisted: Mapping[str, Any] | None = None,
    *,
    last: Mapping[str, Any] | None = None,
) -> None:
    """One scenario: the body as sent, the saved message, and the message it continues."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.sse").write_text(sse, encoding="utf-8")
    if persisted is not None:
        (directory / f"{name}.persisted.json").write_text(_dump(persisted), encoding="utf-8")
    if last is not None:
        (directory / f"{name}.last.json").write_text(_dump(last), encoding="utf-8")


def node_or_skip() -> str:
    """The `node` binary, once it and the installed `ai` are there. Fails under `CI=1`."""
    node = shutil.which("node")
    missing = []
    if node is None:
        missing.append("node is not on PATH")
    if not (REPO_ROOT / "node_modules" / "ai" / "package.json").exists():
        missing.append("node_modules/ai is not installed (run pnpm install)")
    if missing:
        reason = "UI-stream contract check unavailable: " + "; ".join(missing)
        if os.environ.get("CI") == "1":
            pytest.fail(reason)
        warnings.warn(reason, stacklevel=2)
        pytest.skip(reason)
    assert node is not None
    return node


def validate(directory: Path) -> dict[str, Any]:
    """Run the Node check over every case in `directory`; the report, by case name."""
    node = node_or_skip()
    done = subprocess.run(
        [node, str(SCRIPT), str(directory)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=REPO_ROOT,
    )
    if done.returncode not in (0, 1) or not done.stdout:
        raise AssertionError(f"validate_stream.mjs crashed ({done.returncode}):\n{done.stderr}")
    report = json.loads(done.stdout)
    return report["cases"]


def assert_contract(directory: Path) -> dict[str, Any]:
    """`validate`, failing with the details of every case the client would not accept."""
    cases = validate(directory)
    assert cases, f"no .sse cases in {directory}"
    failures = {name: case for name, case in cases.items() if not case["ok"]}
    assert not failures, json.dumps(failures, indent=2, ensure_ascii=False)
    return cases
