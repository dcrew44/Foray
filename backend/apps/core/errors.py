"""The error envelope (design §5.2).

Every failure the API answers is `{ok: false, message}` with one human sentence; nothing
internal leaks, and raw pydantic text is never shown.

| Raised | Answer |
|---|---|
| `ApiError(status, message, **extra)` | that status, `{ok: false, message, **extra}` |
| not signed in (ninja's `AuthenticationError`) | 401 "Not signed in." |
| a validation error in the body or query | 400, the operation's `invalid` message |
| a validation error in a path parameter (a bad id) | 404, the operation's `not_found` message |
| `Http404` | 404 "That no longer exists." |
| anything else | 500, logged with its traceback |

An operation names its own sentences with `@messages(...)`, placed under the router decorator:

    @router.post("/{id}/decision")
    @messages(invalid="That answer could not be read.", not_found="That request no longer exists.")
    def decide(request, id: uuid.UUID, body: DecisionIn): ...

`invalid` may also be a callable `(errors) -> str` for messages built from the failing field.
`UserFacingError` is for background code: its text is shown to the user as is (run failures,
tool results).
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from django.http import Http404, HttpRequest, HttpResponse
from ninja import NinjaAPI
from ninja.errors import AuthenticationError, HttpError, ValidationError

log = logging.getLogger("foray.api")

NOT_SIGNED_IN = "Not signed in."
DEFAULT_INVALID = "That request could not be read."
DEFAULT_NOT_FOUND = "That no longer exists."
UNEXPECTED = "Something went wrong on our side. Try again in a moment."


class ApiError(Exception):
    """An expected failure with the status and sentence the client shows."""

    def __init__(self, status: int, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


class UserFacingError(Exception):
    """A failure whose text is meant for the user verbatim (run failures, tool results)."""


type InvalidMessage = str | Callable[[Sequence[dict[str, Any]]], str]


@dataclass(frozen=True)
class OperationMessages:
    invalid: InvalidMessage = DEFAULT_INVALID
    not_found: str = DEFAULT_NOT_FOUND


def messages[F: Callable[..., Any]](
    *, invalid: InvalidMessage = DEFAULT_INVALID, not_found: str = DEFAULT_NOT_FOUND
) -> Callable[[F], F]:
    """Name the sentences an operation's validation errors become."""

    def deco(fn: F) -> F:
        fn.foray_messages = OperationMessages(invalid, not_found)  # type: ignore[attr-defined]
        return fn

    return deco


def _operation_messages(request: HttpRequest) -> OperationMessages:
    operation = getattr(request, "foray_operation", None)
    view_func = getattr(operation, "view_func", None)
    return getattr(view_func, "foray_messages", None) or OperationMessages()


def remember_operation(run: Callable[..., Any]) -> Callable[..., Any]:
    """A ninja "view" decorator that records the running operation on the request, so the
    validation handler can find that operation's messages. Installed once on the API."""
    operation = getattr(run, "__self__", None)
    if inspect.iscoroutinefunction(run):

        async def arun(request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
            request.foray_operation = operation  # type: ignore[attr-defined]
            return await run(request, *args, **kwargs)

        return arun

    def srun(request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
        request.foray_operation = operation  # type: ignore[attr-defined]
        return run(request, *args, **kwargs)

    return srun


def error_body(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "message": message, **extra}


def install(api: NinjaAPI) -> None:
    """Replace ninja's default handlers with the envelope above."""
    api.add_decorator(remember_operation, mode="view")

    def respond(request: HttpRequest, status: int, message: str, **extra: Any) -> HttpResponse:
        return api.create_response(request, error_body(message, **extra), status=status)

    def on_api_error(request: HttpRequest, exc: ApiError) -> HttpResponse:
        return respond(request, exc.status, exc.message, **exc.extra)

    def on_auth(request: HttpRequest, exc: AuthenticationError) -> HttpResponse:
        return respond(request, 401, NOT_SIGNED_IN)

    def on_http_error(request: HttpRequest, exc: HttpError) -> HttpResponse:
        # ninja raises these itself (403, 429). Ours are ApiError.
        return respond(request, exc.status_code, exc.message)

    def on_validation(request: HttpRequest, exc: ValidationError) -> HttpResponse:
        found = _operation_messages(request)
        if any(error.get("loc", ("",))[0] == "path" for error in exc.errors):
            return respond(request, 404, found.not_found)
        invalid = found.invalid
        return respond(request, 400, invalid if isinstance(invalid, str) else invalid(exc.errors))

    def on_not_found(request: HttpRequest, exc: Http404) -> HttpResponse:
        return respond(request, 404, _operation_messages(request).not_found)

    def on_unexpected(request: HttpRequest, exc: Exception) -> HttpResponse:
        log.exception("%s %s failed", request.method, request.path, exc_info=exc)
        return respond(request, 500, UNEXPECTED)

    handlers: list[tuple[type[Exception], Callable[[HttpRequest, Any], HttpResponse]]] = [
        (ApiError, on_api_error),
        (AuthenticationError, on_auth),
        (HttpError, on_http_error),
        (ValidationError, on_validation),
        (Http404, on_not_found),
        (Exception, on_unexpected),
    ]
    for exc_class, handler in handlers:
        # ninja types handlers as also taking the exception class; it only ever passes instances.
        api.add_exception_handler(exc_class, handler)  # pyright: ignore[reportArgumentType]
