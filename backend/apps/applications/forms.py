"""What an application form asks, read ahead of the user (#60).

Greenhouse publishes each posting's form with the posting: `?questions=true` on the job's own
API address gives every field's name (which is also its id in the page), its label, whether it
is required and, for a choice, the option labels. That is enough to plan the whole application
before anyone opens the page.

Other boards do not publish theirs. For those a `Form` is `complete=False` and holds only the
fields every application has (name, email, phone, resume): the browser extension reads the rest
from the page when it fills it (#22), and asks the same planner for the answers.

Reading a form is one GET through the guarded client. Nothing is ever sent to a board from here.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from apps.core.http import HttpClient, HttpError
from apps.jobs.models import Job

__all__ = ["STANDARD_FIELDS", "Form", "FormField", "FormNotFound", "read_form"]

type FieldType = Literal["text", "textarea", "select", "multiselect", "file", "hidden"]

GREENHOUSE_JOB = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job}?questions=true"
_TAGS = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_GREENHOUSE_TYPES: dict[str, FieldType] = {
    "input_text": "text",
    "textarea": "textarea",
    "input_file": "file",
    "input_hidden": "hidden",
    "multi_value_single_select": "select",
    "multi_value_multi_select": "multiselect",
}


class FormNotFound(Exception):
    """The posting's form is gone (the posting closed)."""


@dataclass(frozen=True, slots=True)
class FormField:
    key: str  # the field's name in the form, which is how the page is filled
    label: str
    type: FieldType
    required: bool
    options: list[str] = field(default_factory=list)  # a choice's option labels, in order

    def dump(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def read(cls, stored: Any) -> FormField | None:
        if not isinstance(stored, dict):
            return None
        try:
            return cls(
                key=str(stored["key"]),
                label=str(stored["label"]),
                type=stored.get("type", "text"),
                required=bool(stored.get("required")),
                options=[str(option) for option in stored.get("options") or []],
            )
        except KeyError:
            return None


@dataclass(frozen=True, slots=True)
class Form:
    source: str  # whose form it is, or "" when unknown
    apply_url: str
    fields: list[FormField]
    # False: only the fields every form has. The page has more, which the extension reads.
    complete: bool


# What every application asks, for a form that cannot be read ahead.
STANDARD_FIELDS: tuple[FormField, ...] = (
    FormField("first_name", "First name", "text", True),
    FormField("last_name", "Last name", "text", True),
    FormField("email", "Email", "text", True),
    FormField("phone", "Phone", "text", False),
    FormField("resume", "Resume", "file", True),
)


def _label(text: Any) -> str:
    """A label as a person reads it: no markup, entities decoded, one line."""
    return _SPACE.sub(" ", _TAGS.sub(" ", html.unescape(str(text or "")))).strip()


def _greenhouse_fields(question: dict[str, Any], prefix: str = "") -> list[FormField]:
    """One Greenhouse question's fields. Most have one; "Resume/CV" offers a file and a text
    box, of which the file is the one to fill."""
    found: list[FormField] = []
    label = _label(question.get("label"))
    for raw in question.get("fields") or []:
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        kind = _GREENHOUSE_TYPES.get(str(raw.get("type")), "text")
        name = str(raw["name"])
        if name.endswith("_text") and any(
            isinstance(other, dict) and other.get("type") == "input_file"
            for other in question.get("fields") or []
        ):
            continue  # the paste-your-resume box beside the upload
        options = [
            _label(value.get("label"))
            for value in raw.get("values") or []
            if isinstance(value, dict) and value.get("label") is not None
        ]
        found.append(
            FormField(
                key=name,
                label=f"{prefix}{label}" if prefix else label,
                type=kind,
                required=bool(question.get("required")),
                options=options,
            )
        )
    return found


def parse_greenhouse(body: str, apply_url: str) -> Form:
    """A Greenhouse job with `?questions=true` as a form: its questions, where it asks for a
    location, and its voluntary disclosures."""
    data = json.loads(body)
    fields: list[FormField] = []
    for question in data.get("questions") or []:
        fields.extend(_greenhouse_fields(question))
    for question in data.get("location_questions") or []:
        fields.extend(_greenhouse_fields(question))
    for block in data.get("compliance") or []:
        for question in (block or {}).get("questions") or []:
            fields.extend(_greenhouse_fields(question))
    for question in (data.get("demographic_questions") or {}).get("questions") or []:
        # The newer demographic block: one choice per question, options under `answer_options`.
        options = [
            _label(option.get("label"))
            for option in question.get("answer_options") or []
            if isinstance(option, dict)
        ]
        fields.append(
            FormField(
                key=f"demographic_{question.get('id')}",
                label=_label(question.get("label")),
                type="multiselect"
                if question.get("type") == "multi_value_multi_select"
                else "select",
                required=bool(question.get("required")),
                options=options,
            )
        )
    url = data.get("absolute_url")
    return Form(
        "greenhouse",
        url if isinstance(url, str) and url else apply_url,
        [item for item in fields if item.type != "hidden"],
        complete=True,
    )


async def read_form(client: HttpClient, job: Job, board_slug: str | None) -> Form:
    """The form of one posting. Raises `FormNotFound` when the posting has gone, and whatever
    else the fetch raises."""
    apply_url = job.apply_url or job.url
    if job.source == "greenhouse" and board_slug:
        url = GREENHOUSE_JOB.format(board=board_slug, job=job.external_id)
        try:
            body = await client.fetch_text(url, timeout=20, retries=1)
        except HttpError as error:
            if error.status == 404:
                raise FormNotFound(job.id) from error
            raise
        return parse_greenhouse(body, apply_url)
    return Form(job.source if job.source != "web" else "", apply_url, list(STANDARD_FIELDS), False)
