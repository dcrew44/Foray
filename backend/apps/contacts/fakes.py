"""The `LLM_FAKE` override this app owns (design §8.9): `outreach`.

Deterministic and free, so a rehearsal can draft a note end to end: the fake reads the same
prompt a model would (`outreach.note_prompt`) and answers with a note that names the contact,
the role and the company from it and says plainly that no model wrote it. The sender's first
name comes from the instructions, where the real prompt puts the sender.

It answers only `OutreachDraft` (told apart by the output schema's title) and returns None for
anything else, so the schema-driven default still answers the rest.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo

from apps.llm import fake

__all__ = ["outreach_reply"]


def _line(text: str, label: str) -> str:
    found = re.search(rf"^{re.escape(label)}: (.*)$", text, re.MULTILINE)
    return found.group(1).strip() if found else ""


def _first(name: str, fallback: str) -> str:
    return name.split()[0] if name.split() else fallback


def outreach_reply(messages: list[ModelMessage], info: AgentInfo) -> dict[str, Any] | None:
    if fake.output_title(info) != "OutreachDraft":
        return None
    prompt = fake.latest_user_text(messages)
    contact = _first(_line(prompt, "Name"), "there")
    title = _line(prompt, "Title") or "the open role"
    # The prompt names the contact's company first and the posting's second.
    companies = re.findall(r"^Company: (.*)$", prompt, re.MULTILINE)
    company = companies[-1].strip() if companies else "your company"
    sender = _first(_line(info.instructions or "", "Name"), "Me")
    return {
        "note": (
            f"{contact}, I saw that {company} is hiring for {title} and I am applying. "
            "This is a fake draft from Foray's test model: no model read your resume or the "
            "posting. Would you be open to telling me what the team is like? "
            f"No worries if not.\n\n{sender}"
        )
    }


fake.register("outreach", outreach_reply)
