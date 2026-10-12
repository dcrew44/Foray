"""The UI-stream contract against the installed `ai` (design §12.4).

Every stream Foray can send is read by the browser's own parser and stream processor, and the
message the client rebuilds must equal the one the server saves. `test_chat_view.py` (step 6)
adds the view's scenarios through the same harness.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.chat.uistream import UIMessageStream
from tests.contract.harness import GOLDENS, assert_contract, validate, write_case
from tests.contract.scenarios import AGENT_SCENARIOS, Played, agent_scenarios, canned

pytestmark = pytest.mark.contract


def _write(directory: Path, played: list[Played]) -> None:
    for p in played:
        write_case(directory, p.name, p.sse, p.message)


def test_ts_reference_turns_validate(tmp_path: Path) -> None:
    """The harness itself: the TS route's own streams pass and rebuild their saved messages."""
    scenarios = json.loads((GOLDENS / "scenarios.json").read_text())["scenarios"]
    for scenario in scenarios:
        name = scenario["name"]
        messages = scenario["uiMessages"]
        last = messages[-1] if messages[-1]["role"] == "assistant" else None
        write_case(
            tmp_path,
            name,
            (GOLDENS / scenario["sse"]).read_text(),
            json.loads((GOLDENS / scenario["persisted"]).read_text()),
            last=last,
        )
    cases = assert_contract(tmp_path)
    assert set(cases) == {s["name"] for s in scenarios}
    assert cases["stream_error"]["errorTexts"] == ["An error occurred."]


def test_canned_streams_validate(tmp_path: Path) -> None:
    """Text, a streamed tool call, a `null` output, an error, an abort and `: ping` comments,
    plus metadata, data parts, input errors, cut-off tool input and reasoning."""
    played = canned()
    _write(tmp_path, played)
    cases = assert_contract(tmp_path)
    for p in played:
        assert cases[p.name]["errorTexts"] == ([p.error_text] if p.error_text else []), p.name
        assert cases[p.name]["matches"] is True, p.name
    assert cases["null_output"]["types"].count("tool-output-available") == 1
    assert "abort" in cases["abort"]["types"]
    assert cases["pings"]["done"] is True


async def test_function_model_agent_validates(tmp_path: Path) -> None:
    """A `FunctionModel` agent run through the real `ForayVercelStream`: every turn validates,
    rebuilds the saved message, and shows exactly the mapped error text."""
    played = await agent_scenarios()
    assert [p.name for p in played] == list(AGENT_SCENARIOS)
    _write(tmp_path, played)
    cases = assert_contract(tmp_path)
    for p in played:
        assert cases[p.name]["errorTexts"] == ([p.error_text] if p.error_text else []), p.name
        assert cases[p.name]["matches"] is True, p.name
        assert "message-metadata" not in cases[p.name]["types"], p.name


def test_contract_rejects_what_the_client_rejects(tmp_path: Path) -> None:
    """The check is not vacuous: each of these fails in the browser, so it fails here."""
    start = 'data: {"type":"start","messageId":"m"}\n\n'
    bad = {
        "null_optional": start + 'data: {"type":"start-step","x":1}\n\n'
        'data: {"type":"text-start","id":"t","providerMetadata":null}\n\n',
        "missing_output": start + 'data: {"type":"start-step"}\n\n'
        'data: {"type":"tool-input-available","toolCallId":"c","toolName":"x","input":{}}\n\n'
        'data: {"type":"tool-output-available","toolCallId":"c"}\n\n',
        "orphan_delta": start + 'data: {"type":"text-delta","id":"t","delta":"x"}\n\n',
        "nan": start + 'data: {"type":"data-x","data":NaN}\n\n',
        "unknown_type": start + 'data: {"type":"tool-input-end","toolCallId":"c"}\n\n',
    }
    for name, sse in bad.items():
        write_case(tmp_path, name, sse + "data: [DONE]\n\n")
    s = UIMessageStream("m")
    s.start()
    s.start_step()
    s.text("hi")
    persisted_too_little = {"id": "m", "role": "assistant", "parts": [{"type": "step-start"}]}
    write_case(tmp_path, "wrong_persisted", s.sse(), persisted_too_little)
    cases = validate(tmp_path)
    assert {name for name, case in cases.items() if case["ok"]} == set()
    assert cases["null_optional"]["rejected"]
    assert cases["missing_output"]["rejected"]
    assert cases["nan"]["rejected"]
    assert cases["unknown_type"]["rejected"]
    assert cases["orphan_delta"]["streamErrors"]
    assert cases["wrong_persisted"]["matches"] is False
