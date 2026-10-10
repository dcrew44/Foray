"""The follow-up notice against the TypeScript goldens (design §7.7, §12.2; the port of
`src/test/unit/follow-up.test.ts`).

`describe_update` is ported verbatim: every golden case gives the same label and text, byte for
byte. Its kind names now come from `runs/catalogue.py`, which adds the two kinds TS lacked.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from apps.chat.follow_up import ApprovalView, EventView, ProposedRole, RunView, describe_update

GOLDEN = Path(__file__).resolve().parents[1] / "goldens" / "strings" / "describe_update.json"
CASES: list[dict[str, Any]] = json.loads(GOLDEN.read_text())


def _update(given: dict[str, Any]) -> tuple[str, str]:
    run = given["run"]
    approval = given.get("approval")
    update = describe_update(
        RunView(id=run["id"], kind=run["kind"], input=run.get("input"), output=run.get("output")),
        EventView(kind=given["event"]["kind"], message=given["event"]["message"]),
        ApprovalView(kind=approval["kind"], title=approval["title"], payload=approval["payload"])
        if approval is not None
        else None,
        [ProposedRole(**role) for role in given.get("proposed") or []],
    )
    return update.label, update.text


def test_every_golden_case_is_present() -> None:
    assert len(CASES) == 21


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_describe_update_matches_the_golden(case: dict[str, Any]) -> None:
    label, text = _update(case["input"])
    assert label == case["output"]["label"]
    assert text == case["output"]["text"]


def _search(**output: Any) -> RunView:
    return RunView(id="run-9", kind="search", input={"prompt": "find roles"}, output=output)


def test_the_promised_phrases() -> None:
    """The substrings `follow-up.test.ts` asserted on."""
    finished = describe_update(_search(candidates=3), EventView("run.completed", "Found 3."), None)
    assert 'Call list_jobs with run_id "run-9"' in finished.text
    assert "limit 5" in finished.text
    assert "can only look things up" in finished.text
    empty = describe_update(_search(candidates=0), EventView("run.completed", "Nothing."), None)
    assert "widen the search" in empty.text
    failed = describe_update(_search(), EventView("run.failed", "Boom."), None)
    assert failed.label == "Search stopped" and "offer to try again" in failed.text
    gate = describe_update(
        _search(),
        EventView("approval.requested", "Waiting."),
        ApprovalView("deep_eval_batch", "Deep-evaluate 2 roles", {"items": []}),
    )
    assert "do not call list_jobs" in gate.text
    assert "Do not answer the approval yourself" in gate.text


def test_only_the_number_zero_is_an_empty_search() -> None:
    for candidates in (False, "0", None):
        update = describe_update(
            _search(candidates=candidates), EventView("run.completed", "Done."), None
        )
        empty = "widen the search" in update.text
        assert empty is (candidates is None), candidates


@pytest.mark.parametrize(
    ("kind", "label"),
    [("company_refresh", "Board refresh finished"), ("profile_import", "Resume import finished")],
)
def test_the_kinds_typescript_lacked(kind: str, label: str) -> None:
    update = describe_update(
        RunView(id="run-1", kind=kind, input={}, output=None),
        EventView("run.completed", "Done."),
        None,
    )
    assert update.label == label
    name = "A board refresh" if kind == "company_refresh" else "Reading a resume"
    assert f"{name} has finished: Done." in update.text


@pytest.mark.parametrize(
    ("kind", "run_kind"),
    [
        ("deep_eval_batch", "search"),
        ("shortlist_proposal", "search"),
        ("deep_eval_batch", "tailor"),
    ],
)
def test_a_gate_update_names_its_approval(kind: str, run_kind: str) -> None:
    """Every update for a waiting approval says which one it is (its id, kind and title), right
    before the rules, so a later "yes" in chat answers it without guessing. The goldens are
    built without an id and stay byte for byte the TypeScript text."""
    approval = ApprovalView(kind, "Shortlist 2 roles", {"items": []}, id="0b5d7c1e")
    update = describe_update(
        RunView(id="run-9", kind=run_kind, input={"prompt": "find roles"}, output=None),
        EventView("approval.requested", "Waiting."),
        approval,
    )
    lines = update.text.splitlines()
    assert lines[-2] == (
        f'The pending approval is "0b5d7c1e" ({kind}: Shortlist 2 roles). When the user answers '
        "it in chat, pass that id to answer_approval; never guess one."
    )
    without = describe_update(
        RunView(id="run-9", kind=run_kind, input={"prompt": "find roles"}, output=None),
        EventView("approval.requested", "Waiting."),
        ApprovalView(kind, "Shortlist 2 roles", {"items": []}),
    )
    assert without.text.splitlines() == lines[:-2] + lines[-1:]
