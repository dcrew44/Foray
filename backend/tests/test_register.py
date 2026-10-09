"""The invariant register and the conventions a type checker cannot see (DESIGN.md).

1. **Register.** Parses the "Invariant register" table of DESIGN.md and checks that every test
   named in its "Proved by" column exists. A row whose test *file* does not exist is reported as
   pending (a skip), unless `FORAY_REGISTER_STRICT=1`, which turns it into a failure; a file
   that exists but lacks the named test always fails.
2. **Conventions.** Greps the code of `apps/` and `foray/` (comments and string literals are
   ignored) and fails on:
   - `sync_to_async` outside `apps/core/db.py` and `apps/chat/views.py`;
   - an `httpx2` import outside `apps/core/http.py`;
   - `._plain` or `_default_manager` outside `apps/core/scoping.py`, `apps/legacy/`,
     `apps/runs/reaper.py` and migrations;
   - `get_object_or_404(` called with a per-user (`UserOwned`) model;
   - a tool in `apps/chat/tools.py` that is not `async def`;
   - `.defer(`, `.defer_async(` or `defer_job` outside `apps/core/queue.py`;
   - `Router(` outside `apps/core/router.py`;
   - `auto_now` anywhere.
"""

from __future__ import annotations

import ast
import io
import os
import re
import token
import tokenize
from dataclasses import dataclass
from pathlib import Path

import pytest
from django.apps import apps as django_apps

BACKEND = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND.parent
TESTS = BACKEND / "tests"
REGISTER_SOURCE = REPO_ROOT / "DESIGN.md"
REGISTER_HEADING = "## Invariant register"
STRICT = os.environ.get("FORAY_REGISTER_STRICT") == "1"
SCANNED = ("apps", "foray")


# --- 1. the register ------------------------------------------------------------------------


@dataclass(frozen=True)
class Proof:
    file: str  # relative to backend/tests
    test: str | None  # None: the whole file proves the row


def parse_register(text: str) -> dict[str, list[Proof]]:
    """Rows of the `## Invariant register` table: {"R1": [Proof, ...], ...}."""
    section = text.split(REGISTER_HEADING, 1)[1].split("\n## ", 1)[0]
    rows: dict[str, list[Proof]] = {}
    for line in section.splitlines():
        match = re.match(r"^\|\s*(R\d+)\s*\|", line)
        if not match:
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        proofs: list[Proof] = []
        current: str | None = None
        for item in re.findall(r"`([^`]+)`", cells[-1]):
            path, _, name = item.partition("::")
            if path:
                current = path
            if current is None or not current.endswith(".py"):
                continue
            proofs.append(Proof(current, name or None))
        rows[match.group(1)] = proofs
    return rows


REGISTER = parse_register(REGISTER_SOURCE.read_text())


def test_register_is_complete() -> None:
    assert list(REGISTER) == [f"R{n}" for n in range(1, len(REGISTER) + 1)]
    assert len(REGISTER) >= 17
    assert all(REGISTER.values()), "every invariant names at least one test"


def _defined_tests(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith(
            "test"
        ):
            names.add(node.name)
    return names


@pytest.mark.parametrize("row", list(REGISTER))
def test_register_row_is_proved(row: str) -> None:
    pending: list[str] = []
    for proof in REGISTER[row]:
        path = TESTS / proof.file
        if not path.exists():
            pending.append(proof.file)
            continue
        if proof.test is not None:
            assert proof.test in _defined_tests(path), (
                f"{row}: tests/{proof.file} exists but has no {proof.test}"
            )
    if pending:
        message = f"{row} pending: " + ", ".join(f"tests/{p}" for p in sorted(set(pending)))
        if STRICT:
            pytest.fail(message)
        pytest.skip(message)


# --- 2. conventions -------------------------------------------------------------------------

_MASKED = {token.COMMENT, token.STRING, token.FSTRING_MIDDLE}
if hasattr(token, "TSTRING_MIDDLE"):
    _MASKED.add(token.TSTRING_MIDDLE)


def code_only(source: str) -> str:
    """The source with comments and string literals blanked out, line structure kept."""
    lines = source.splitlines(keepends=True)
    masked = [list(line) for line in lines]
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type not in _MASKED:
            continue
        (srow, scol), (erow, ecol) = tok.start, tok.end
        for row in range(srow, erow + 1):
            chars = masked[row - 1]
            start = scol if row == srow else 0
            end = ecol if row == erow else len(chars)
            for col in range(start, end):
                if chars[col] != "\n":
                    chars[col] = " "
    return "".join("".join(chars) for chars in masked)


def _sources() -> list[tuple[str, str]]:
    found = []
    for top in SCANNED:
        for path in sorted((BACKEND / top).rglob("*.py")):
            rel = path.relative_to(BACKEND).as_posix()
            found.append((rel, code_only(path.read_text())))
    return found


SOURCES = _sources()


def _violations(pattern: str, allowed: tuple[str, ...] = ()) -> list[str]:
    regex = re.compile(pattern)
    hits = []
    for rel, code in SOURCES:
        if any(rel == a or (a.endswith("/") and rel.startswith(a)) for a in allowed):
            continue
        for number, line in enumerate(code.splitlines(), 1):
            if regex.search(line):
                hits.append(f"{rel}:{number}: {line.strip()}")
    return hits


def _is_migration(rel: str) -> bool:
    return "/migrations/" in rel


def test_sync_to_async_only_in_the_bridge() -> None:
    assert _violations(r"\bsync_to_async\b", ("apps/core/db.py", "apps/chat/views.py")) == []


def test_httpx2_only_in_the_http_module() -> None:
    assert _violations(r"^\s*(import\s+httpx2\b|from\s+httpx2\b)", ("apps/core/http.py",)) == []


def test_plain_managers_stay_internal() -> None:
    hits = _violations(
        r"\._plain\b|\b_default_manager\b",
        ("apps/core/scoping.py", "apps/legacy/", "apps/runs/reaper.py"),
    )
    assert [hit for hit in hits if not _is_migration(hit.split(":", 1)[0])] == []


def test_get_object_or_404_never_takes_a_per_user_model() -> None:
    per_user = {model.__name__ for model in django_apps.get_models() if hasattr(model, "_plain")}
    hits = []
    for rel, code in SOURCES:
        for match in re.finditer(r"\bget_object_or_404\(\s*([A-Za-z_][\w.]*)", code):
            if match.group(1).rsplit(".", 1)[-1] in per_user:
                hits.append(f"{rel}: get_object_or_404({match.group(1)}, ...)")
    assert hits == [], "use Model.objects.for_user(uid) and raise ApiError(404, ...)"


def test_chat_tools_are_async() -> None:
    tools = BACKEND / "apps" / "chat" / "tools.py"
    if not tools.exists():
        pytest.skip("apps/chat/tools.py has not landed yet")
    tree = ast.parse(tools.read_text())
    sync_tools = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
            if "tool" in name.lower():
                sync_tools.append(node.name)
    assert sync_tools == [], "chat tools must be async def and reach the database through db()"


def test_every_defer_goes_through_defer_in_tx() -> None:
    assert _violations(r"\.defer\(|\.defer_async\(|\bdefer_job\b", ("apps/core/queue.py",)) == []


def test_every_router_comes_from_the_factory() -> None:
    assert _violations(r"\bRouter\(", ("apps/core/router.py",)) == []


def test_no_auto_now() -> None:
    assert _violations(r"\bauto_now(_add)?\b") == []


def test_code_only_masks_comments_and_strings() -> None:
    sample = 'x = "auto_now"  # Router(\ny = f"{z} .defer("\nRouter()\n'
    masked = code_only(sample).splitlines()
    assert "auto_now" not in masked[0] and "Router(" not in masked[0]
    assert ".defer(" not in masked[1]
    assert masked[2] == "Router()"
