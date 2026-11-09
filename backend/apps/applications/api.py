"""`/api/answers`: the user's saved answers to the questions application forms share (#21).

One `PUT` saves any number of answers at once (the Profile page's form, or one inline answer on
the Apply page); an empty answer forgets it. The answers themselves are read with the pages that
show them.
"""

from __future__ import annotations

from typing import Any, cast

from django.http import HttpRequest
from pydantic import Field

from apps.accounts.models import User
from apps.applications import answers as saved_answers
from apps.applications.models import AnswerKind
from apps.core.errors import ApiError, messages
from apps.core.router import ApiModel, router
from apps.core.schemas import OkMessageOut

answers = router(tags=["answers"])

ROUTERS = {"/answers": answers}

ANSWERS_INVALID = "Those answers could not be read."
ANSWERS_SAVED = "Answers saved"
MAX_AT_ONCE = 60


class AnswerIn(ApiModel):
    kind: AnswerKind
    answer: str = Field(max_length=saved_answers.MAX_ANSWER)
    # The question's own words: required for a custom answer, ignored for the others.
    question: str = Field(default="", max_length=saved_answers.MAX_QUESTION)


class AnswersIn(ApiModel):
    answers: list[AnswerIn] = Field(max_length=MAX_AT_ONCE)


@answers.put("", response=OkMessageOut, summary="Save answers")
@messages(invalid=ANSWERS_INVALID)
def put_answers(request: HttpRequest, body: AnswersIn) -> dict[str, Any]:
    user_id = cast(User, request.user).pk
    for item in body.answers:
        try:
            saved_answers.save(user_id, item.kind, item.answer, item.question)
        except ValueError:
            raise ApiError(400, ANSWERS_INVALID) from None
    return {"ok": True, "message": ANSWERS_SAVED}
