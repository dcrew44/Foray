"""The follow-up notice against the TypeScript goldens (design §7.7, §12.2; the port of
`src/test/unit/follow-up.test.ts`).

`describe_update` is ported verbatim: every golden case gives the same label and text, byte for
byte, but for `ALLOWED_DIFFS`. Its kind names now come from `runs/catalogue.py`, which adds the
two kinds TS lacked.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from apps.chat.follow_up import ON_THE_CARD as ON_THE_CARD_TEXT
from apps.chat.follow_up import ApprovalView, EventView, ProposedRole, RunView, describe_update

GOLDEN = Path(__file__).resolve().parents[1] / "goldens" / "strings" / "describe_update.json"
CASES: list[dict[str, Any]] = json.loads(GOLDEN.read_text())

# The declared differences.
# #30: the roles a gate asks about are third-party text (titles, companies, places and the
# reviews' headlines), so their list is wrapped. It opens after its lead-in and closes before the
# line that follows it.
ROLE_LIST_LEADS = ("(fast score in brackets):\n", "verdict:\n")
AFTER_ROLE_LIST = "\nThe approval card above already lists"
# #31: chat has no answer_approval, so a waiting approval is answered on its card.
ON_THE_CARD = [
    ("Do not answer the approval yourself.", ON_THE_CARD_TEXT),
    ("Say what it needs and ask.", f"Say what it needs and ask. {ON_THE_CARD_TEXT}"),
]


def _declared(text: str) -> str:
    for lead in ROLE_LIST_LEADS:
        if f"{lead}- " in text:
            assert text.count(AFTER_ROLE_LIST) == 1
            text = text.replace(f"{lead}- ", f'{lead}<untrusted source="job postings">\n- ')
            text = text.replace(AFTER_ROLE_LIST, f"\n</untrusted>{AFTER_ROLE_LIST}")
    for before, after in ON_THE_CARD:
        assert text.count(before) <= 1, before
        text = text.replace(before, after)
    return text


def test_every_declared_difference_is_used() -> None:
    texts = [case["output"]["text"] for case in CASES]
    for lead in ROLE_LIST_LEADS:
        assert any(f"{lead}- " in text for text in texts), lead
    for before, _ in ON_THE_CARD:
        assert any(before in text for text in texts), before


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
    assert text == _declared(case["output"]["text"])


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
    assert "They answer on the card above; you cannot answer it for them." in gate.text


def test_a_watch_reports_its_new_matches() -> None:
    """#5: only a watch with something to announce reports to chat, in the "New matches" chat."""
    update = describe_update(
        RunView(id="run-7", kind="watch", input={"prompt": "Check new postings"}, output={}),
        EventView("run.completed", "2 roles look worth applying to, from 9 new postings."),
        None,
    )
    assert update.label == "New matches"
    assert update.text.startswith("[Automatic update about run run-7, not written by the user]")
    assert "Its result: 2 roles look worth applying to, from 9 new postings." in update.text
    assert 'Call list_jobs with run_id "run-7"' in update.text
    assert "applying early" in update.text
    assert "can only look things up" in update.text


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
        ("run_start", "tailor"),
    ],
)
def test_a_gate_update_sends_the_user_to_the_card(kind: str, run_kind: str) -> None:
    """#31: chat cannot answer an approval, so every update for one says where the user does,
    and no longer names an id for answer_approval."""
    update = describe_update(
        RunView(id="run-9", kind=run_kind, input={"prompt": "find roles"}, output=None),
        EventView("approval.requested", "Waiting."),
        ApprovalView(kind, "Shortlist 2 roles", {"items": []}),
    )
    assert "They answer on the card above; you cannot answer it for them." in update.text
    assert "answer_approval" not in update.text
