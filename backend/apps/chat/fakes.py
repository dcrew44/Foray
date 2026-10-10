"""The `chat` fake (design §8.9; `LLM_FAKE=1`, DEBUG only).

It routes on what the request offers, never on keywords alone: it reads `info.function_tools`
(the tools this turn exposes, which a follow-up or the last capped step narrows) and the latest
user prompt, and plays one short plan per kind of request:

| The prompt | The plan |
|---|---|
| a notice, `[Automatic update about run <id>, …` | `get_run_status`, then two sentences |
| "set up" (Home's "Answer in chat") | `get_profile_summary`, then the interview's question |
| labelled answers, `Roles: …; Level: …` | `update_profile_preferences`, then a sentence |
| "find" / "search" | `start_search_run`, then a sentence |
| "yes" / "approve" | `list_pending_approvals`, `answer_approval` (the first), a sentence |
| "add <name>" | `add_company`, then a sentence |
| "matches" / "jobs" | `list_jobs` with every argument null, then a sentence |
| "resume" | `get_resume`, then a sentence |
| anything else | a short text reply |

A wanted tool that is not offered (a read-only follow-up, the step cap) degrades to text, never
to a call of an unknown tool. A structured request on the `chat` purpose (`discover_companies`'
name proposal) is left to the schema-driven default.

"add <name>" is read before "matches"/"jobs": a board link such as
`add https://jobs.ashbyhq.com/linear` contains "jobs" and must still add the company.

The interview plans let the onboarding flow run on fakes (acceptance item 2): "set up" comes
before "search" because Home's opener ("Let's set up my job search…") contains both. Answers are
read only in the labelled form `<label>: <value>`, separated by `;` or new lines, with list
values separated by commas: `Roles: Software Engineer, Backend Engineer; Level: entry;
Locations: San Francisco Bay Area; Remote: hybrid; Work authorisation: US citizen`. A value the
schema does not accept (a level that is not one of the options) is left out, never guessed.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo

from apps.llm import fake

NOTICE = re.compile(r"^\[Automatic update about run ([^,\]\s]+)")
INTERVIEW = re.compile(r"\bset\s+up\b", re.IGNORECASE)
ANSWER = re.compile(r"^\s*([A-Za-z][A-Za-z -]*?)\s*:\s*(.+?)\s*$")
SEARCH = re.compile(r"\b(find|search)\b", re.IGNORECASE)
APPROVE = re.compile(r"\b(yes|approve)\b", re.IGNORECASE)
JOBS = re.compile(r"\b(matches|jobs)\b", re.IGNORECASE)
RESUME = re.compile(r"\bresume\b", re.IGNORECASE)
ADD = re.compile(r"\badd\s+(.+)", re.IGNORECASE | re.DOTALL)

LIST_JOBS_ARGS: dict[str, Any] = {
    "run_id": None,
    "limit": None,
    "stage": None,
    "min_score": None,
    "query": None,
}
JOBS_LISTED = "Here are your matches."
FALLBACK = "This is a fake reply from Foray's test model. Nothing was sent to a provider."
INTERVIEW_QUESTION = (
    "Let's set up your search. Tell me the roles you want, your level, where you want to work, "
    "whether you want remote, hybrid or on-site work, and your work authorisation."
)

# `<label>: <value>` → (preference field, how the value is read). Labels are lowercased with
# their spaces and hyphens collapsed.
type Reader = Callable[[str], Any]


def _words(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _choice(options: tuple[str, ...]) -> Reader:
    def read(value: str) -> str | None:
        key = re.sub(r"[\s-]+", "_", value.strip().lower())
        key = {"entry_level": "entry", "on_site": "onsite", "in_office": "onsite"}.get(key, key)
        return key if key in options else None

    return read


SENIORITY = ("intern", "new_grad", "entry", "mid", "senior", "staff_plus")
REMOTE = ("remote", "hybrid", "onsite", "any")
ANSWER_FIELDS: dict[str, tuple[str, Reader]] = {
    "roles": ("target_roles", _words),
    "target roles": ("target_roles", _words),
    "level": ("seniority", _choice(SENIORITY)),
    "seniority": ("seniority", _choice(SENIORITY)),
    "locations": ("locations", _words),
    "location": ("locations", _words),
    "remote": ("remote_pref", _choice(REMOTE)),
    "work authorisation": ("work_auth", str.strip),
    "work authorization": ("work_auth", str.strip),
    "work auth": ("work_auth", str.strip),
    "start date": ("start_date", str.strip),
    "must haves": ("must_haves", _words),
    "dealbreakers": ("dealbreakers", _words),
}
PREFERENCE_ARGS: tuple[str, ...] = (
    "target_roles",
    "seniority",
    "locations",
    "remote_pref",
    "salary_floor",
    "work_auth",
    "start_date",
    "must_haves",
    "dealbreakers",
    "declined_slots",
)


def read_answers(prompt: str) -> dict[str, Any] | None:
    """The `update_profile_preferences` arguments a labelled answer gives, or None when the
    prompt has no recognised `<label>: <value>`."""
    found: dict[str, Any] = {}
    for line in re.split(r"[;\n]", prompt):
        matched = ANSWER.match(line)
        if not matched:
            continue
        label = re.sub(r"[\s-]+", " ", matched.group(1).strip().lower())
        if label not in ANSWER_FIELDS:
            continue
        field, read = ANSWER_FIELDS[label]
        value = read(matched.group(2))
        if value:
            found[field] = value
    if not found:
        return None
    return {name: found.get(name) for name in PREFERENCE_ARGS}


type Done = dict[str, Any]  # tool name -> what it returned, this turn
type Args = Callable[[Done], dict[str, Any] | None]  # None: nothing to call it with


@dataclass(frozen=True, slots=True)
class Plan:
    steps: tuple[tuple[str, Args], ...]
    reply: Callable[[Done], str]


def _prompt_index(messages: list[ModelMessage]) -> int:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, ModelRequest) and any(
            isinstance(part, UserPromptPart) for part in message.parts
        ):
            return index
    return -1


def _done(messages: list[ModelMessage]) -> Done:
    """What each tool called since the latest prompt returned."""
    start = _prompt_index(messages)
    called: dict[str, str] = {}
    done: Done = {}
    for message in messages[start + 1 :]:
        if isinstance(message, ModelResponse):
            for part in message.parts:
                if isinstance(part, ToolCallPart):
                    called[part.tool_call_id] = part.tool_name
        elif isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, ToolReturnPart) and part.tool_call_id in called:
                    done[called[part.tool_call_id]] = part.content
    return done


def _get(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, Mapping) else None


def _plan(prompt: str) -> Plan:
    notice = NOTICE.match(prompt)
    if notice:
        run_id = notice.group(1)

        def report(done: Done) -> str:
            status = _get(done.get("get_run_status"), "status")
            first = (
                f"Foray checked the run: it is {status}."
                if status
                else ("Here is an update on that run.")
            )
            return f"{first} Tell me what you would like to do next."

        return Plan((("get_run_status", lambda done: {"run_id": run_id}),), report)

    if INTERVIEW.search(prompt):
        return Plan((("get_profile_summary", lambda done: {}),), lambda done: INTERVIEW_QUESTION)

    answers = read_answers(prompt)
    if answers is not None:

        def saved(done: Done) -> str:
            result = done.get("update_profile_preferences")
            if _get(result, "ok") is False:
                return str(_get(result, "message") or "Those answers could not be saved.")
            blocking = _get(result, "blockingSearch")
            if isinstance(blocking, list) and blocking:
                return f"Saved. Foray still needs your {', '.join(map(str, blocking)).lower()}."
            return "Saved. Foray has what it needs to search."

        return Plan((("update_profile_preferences", lambda done: dict(answers)),), saved)

    if SEARCH.search(prompt):
        search_args = {
            "prompt": prompt,
            "companies": None,
            "roles": None,
            "seniority": None,
            "locations": None,
            "remote": None,
            "focus": None,
            "mode": None,
        }

        def started(done: Done) -> str:
            result = done.get("start_search_run")
            if _get(result, "ok") is False:
                return str(_get(result, "message") or "The search could not start.")
            return "I started a search. I will report back here when it needs you or is done."

        return Plan((("start_search_run", lambda done: search_args),), started)

    if APPROVE.search(prompt):

        def answer(done: Done) -> dict[str, Any] | None:
            approvals = _get(done.get("list_pending_approvals"), "approvals")
            if not isinstance(approvals, list) or not approvals:
                return None
            return {"approval_id": _get(approvals[0], "id"), "approve": True, "item_ids": None}

        def answered(done: Done) -> str:
            if "answer_approval" in done:
                return str(_get(done["answer_approval"], "message") or "Answered.")
            return "Nothing is waiting on you."

        return Plan(
            (("list_pending_approvals", lambda done: {}), ("answer_approval", answer)), answered
        )

    added = ADD.search(prompt)
    if added:
        name = added.group(1).strip().rstrip(".!?").strip()

        def looking(done: Done) -> str:
            result = done.get("add_company")
            if _get(result, "ok") is False:
                return str(_get(result, "message") or "That company could not be added.")
            return f"I am looking for a job board for {name}. I will report back here when done."

        return Plan((("add_company", lambda done: {"name_or_url": name}),), looking)

    if JOBS.search(prompt):

        def listed(done: Done) -> str:
            result = done.get("list_jobs")
            if _get(result, "ok") is False:
                return str(_get(result, "message") or "I could not list your matches.")
            return JOBS_LISTED

        return Plan((("list_jobs", lambda done: dict(LIST_JOBS_ARGS)),), listed)

    if RESUME.search(prompt):

        def summary(done: Done) -> str:
            result = done.get("get_resume")
            text = _get(result, "resume")
            if isinstance(text, str) and text:
                return f"Here is the top of your resume: {text.splitlines()[0]}."
            return str(_get(result, "message") or "I could not read your resume.")

        return Plan((("get_resume", lambda done: {"sections": None}),), summary)

    return Plan((), lambda done: FALLBACK)


@fake.register("chat")
def chat_reply(messages: list[ModelMessage], info: AgentInfo) -> fake.Reply:
    if info.output_tools and not info.allow_text_output:
        return None  # a structured call (discover_companies' proposal): the default answers
    plan = _plan(fake.latest_user_text(messages).strip())
    offered = fake.offered_tools(info)
    done = _done(messages)
    for name, args in plan.steps:
        if name in done:
            continue
        if name not in offered:
            break  # not offered this turn: answer in text instead
        arguments = args(done)
        if arguments is None:
            break
        return fake.tool_call(name, arguments)
    return plan.reply(done)
