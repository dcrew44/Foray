"""Accounts endpoints (design §4.4, §5.1, §5.3).

| Endpoint | Auth | Answer |
|---|---|---|
| `POST /api/auth/sign-up` | none | `{ok, user}` plus a session |
| `POST /api/auth/sign-in` | none | `{ok, user}` plus a session (`login()` rotates the key) |
| `POST /api/auth/sign-out` | session | `{ok}`; the session is flushed |
| `GET /api/auth/config` | none | `{signupOpen}`, for the sign-in page |
| `GET /api/me` | session | `{id, name, email, signupOpen}` |
| `PUT /api/preferences` | session | `{ok, message}`; works before any resume exists |
| `PATCH /api/settings` | session | `{ok, message, settings}` |

401 means only "not signed in": wrong credentials are a 400 with Better Auth's message, so the
client's 401 redirect can never swallow a sign-in error. The user-visible strings are today's
(`map/auth.md` §7, `map/http-surface.md` §4).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Annotated, Any, cast

from django.conf import settings as django_settings
from django.contrib.auth import authenticate, login, logout
from django.db import IntegrityError, transaction
from django.http import HttpRequest
from pydantic import BeforeValidator, ConfigDict, Field, model_validator
from pydantic.json_schema import SkipJsonSchema

from apps.accounts import profile
from apps.accounts.hashers import PASSWORD_MAX, PASSWORD_MIN, js_length
from apps.accounts.models import User, normalize_email_address
from apps.accounts.schemas import AutonomyMode, Preferences, RemotePreference, Seniority
from apps.core import throttle
from apps.core.errors import DEFAULT_INVALID, ApiError, messages
from apps.core.openapi import register_schema
from apps.core.router import ApiModel, router
from foray.middleware import stamp_session

auth = router(tags=["auth"])
me = router(tags=["me"])
preferences = router(tags=["preferences"])
settings = router(tags=["settings"])

ROUTERS = {"/auth": auth, "/me": me, "/preferences": preferences, "/settings": settings}

MODEL_BACKEND = "django.contrib.auth.backends.ModelBackend"

INVALID_EMAIL = "Invalid email"
INVALID_CREDENTIALS = "Invalid email or password"
PASSWORD_TOO_SHORT = "Password too short"
PASSWORD_TOO_LONG = "Password too long"
USER_EXISTS = "User already exists. Use another email."
SIGNUPS_CLOSED = "Sign-ups are closed."
PREFERENCES_SAVED = "Preferences saved"
PREFERENCES_INVALID = "Those preferences could not be saved. Check the fields and try again."
SETTINGS_SAVED = "Settings saved"
SETTINGS_INVALID = "Those settings are out of range. Check the values and try again."

EMAIL_MAX = 320  # users.email
NAME_MAX = 200  # users.name

# zod 4.6's `z.email()` (`zod/v4/core/regexes.js`), which Better Auth applies to sign-up and
# sign-in. ASCII only, as the JavaScript regex has no `u` flag; `fullmatch` because Python's `$`
# would also accept a trailing newline.
EMAIL_PATTERN = re.compile(
    r"(?:[A-Za-z0-9_'+\-]+\.)*[A-Za-z0-9_'+\-]*[A-Za-z0-9_+-]@"
    r"(?:[A-Za-z0-9][A-Za-z0-9\-]*\.)+[A-Za-z]{2,}",
    re.ASCII,
)

# The frontend gets these as named types (§5.4). Operations already document the models they
# take and return; the profile page serves `SLOTS` and the onboarding state.
for _name, _type in (
    ("Seniority", Seniority),
    ("RemotePreference", RemotePreference),
    ("AutonomyMode", AutonomyMode),
    ("SlotId", profile.SlotId),
    ("PreferenceField", profile.PreferenceField),
):
    register_schema(_type, _name)
register_schema(profile.Slot)
register_schema(profile.InterviewProgress)


def is_valid_email(email: str) -> bool:
    return len(email) <= EMAIL_MAX and EMAIL_PATTERN.fullmatch(email) is not None


def current_user(request: HttpRequest) -> User:
    """The signed-in user of an authenticated operation."""
    return cast(User, request.user)


def _start_session(request: HttpRequest, user: User) -> None:
    login(request, user, backend=MODEL_BACKEND)
    # A fresh login needs no slide for a day (foray.middleware.sliding_session).
    stamp_session(request.session)


# --- schemas -------------------------------------------------------------------------------------


class SignUpIn(ApiModel):
    name: str | None = None
    email: str
    password: str


class SignInIn(ApiModel):
    email: str
    password: str


class AuthUser(ApiModel):
    id: str
    name: str
    email: str


class AuthOut(ApiModel):
    ok: bool
    user: AuthUser


class OkOut(ApiModel):
    ok: bool


class OkMessageOut(ApiModel):
    ok: bool
    message: str


class ConfigOut(ApiModel):
    signup_open: bool


class MeOut(ApiModel):
    id: str
    name: str
    email: str
    signup_open: bool


def _json_number(value: Any) -> Any:
    # zod's z.number() takes numbers only: pydantic's lax int would also take "55" and true.
    if isinstance(value, bool | str):
        raise ValueError("not a number")
    return value


# The bounds first, so the JSON schema states them as minimum/maximum.
ScoreThreshold = Annotated[int, Field(ge=0, le=100), BeforeValidator(_json_number)]
DeepEvalTopN = Annotated[int, Field(ge=0, le=60), BeforeValidator(_json_number)]


class SettingsPatch(ApiModel):
    """Any subset of the three settings. Unknown keys and nulls are refused."""

    # `SettingsSchema.partial().strict()` in the TypeScript action.
    model_config = ConfigDict(extra="forbid")

    default_mode: AutonomyMode | SkipJsonSchema[None] = None
    score_threshold: ScoreThreshold | SkipJsonSchema[None] = None
    deep_eval_top_n: DeepEvalTopN | SkipJsonSchema[None] = None

    @model_validator(mode="after")
    def _no_nulls(self) -> SettingsPatch:
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("a setting cannot be null")
        return self


class SettingsOut(ApiModel):
    """The user's settings with the defaults applied: every field is always present."""

    default_mode: AutonomyMode
    score_threshold: int
    deep_eval_top_n: int


class SettingsSaved(ApiModel):
    ok: bool
    message: str
    settings: SettingsOut


# --- validation messages -------------------------------------------------------------------------


def _body_loc(error: dict[str, Any]) -> tuple[Any, ...]:
    """An error's location inside the request body (ninja prefixes `("body", <param>)`)."""
    loc = tuple(error.get("loc", ()))
    if loc[:1] == ("body",):
        loc = loc[2:]
    return loc


def _email_or(default: str):
    def invalid(errors: Sequence[dict[str, Any]]) -> str:
        if any(_body_loc(error)[:1] == ("email",) for error in errors):
            return INVALID_EMAIL
        return default

    return invalid


_PROBLEM_BY_TYPE = {
    "list_type": "it should be a list",
    "string_type": "it should be text",
    "literal_error": "that is not one of the options",
    "enum": "that is not one of the options",
    "int_type": "the amount should be a number",
    "int_parsing": "the amount should be a number",
    "float_type": "the amount should be a number",
    "float_parsing": "the amount should be a number",
    "finite_number": "the amount should be a number",
}


def preferences_invalid(errors: Sequence[dict[str, Any]]) -> str:
    """`"<Slot label> could not be saved: <problem>."` for the first failing field, as
    `savePreferencesAction` answered (§5.2); never pydantic's own text."""
    if not errors:
        return PREFERENCES_INVALID
    loc = _body_loc(errors[0])
    slot = profile.slot_for_field(str(loc[0])) if loc else None
    if slot is None:
        return PREFERENCES_INVALID
    error_type = str(errors[0].get("type", ""))
    if error_type == "missing" and len(loc) > 1:
        problem = f"the {loc[-1]} is missing"
    else:
        problem = _PROBLEM_BY_TYPE.get(error_type, "the value is not valid")
    return f"{slot.label} could not be saved: {problem}."


# --- /auth ---------------------------------------------------------------------------------------


def _auth_out(user: User) -> dict[str, Any]:
    return {"ok": True, "user": {"id": user.pk, "name": user.name, "email": user.email}}


@auth.post("/sign-up", response=AuthOut, auth=None, summary="Sign up")
@messages(invalid=_email_or(DEFAULT_INVALID))
def sign_up(request: HttpRequest, body: SignUpIn) -> dict[str, Any]:
    if not django_settings.ENV.signup_open:
        raise ApiError(403, SIGNUPS_CLOSED)
    if not is_valid_email(body.email):
        raise ApiError(400, INVALID_EMAIL)
    length = js_length(body.password)
    if length < PASSWORD_MIN:
        raise ApiError(400, PASSWORD_TOO_SHORT)
    if length > PASSWORD_MAX:
        raise ApiError(400, PASSWORD_TOO_LONG)

    # Counted in autocommit, before the duplicate check (which would otherwise answer "does this
    # email have an account?" without limit).
    throttle.throttle(
        f"signup:{throttle.client_key(request)}", throttle.SIGNUP.limit, throttle.SIGNUP.window_s
    )

    email = normalize_email_address(body.email)
    # The form sends `name || email.split("@")[0]`; an API caller may leave it out.
    name = body.name if body.name and body.name.strip() else body.email.split("@", 1)[0]
    if User.objects.filter(email=email).exists():
        raise ApiError(409, USER_EXISTS)
    try:
        # One transaction: Better Auth's user and account inserts were not atomic.
        with transaction.atomic():
            user = User.objects.create_user(email, password=body.password, name=name[:NAME_MAX])
    except IntegrityError:
        raise ApiError(409, USER_EXISTS) from None
    _start_session(request, user)
    return _auth_out(user)


@auth.post("/sign-in", response=AuthOut, auth=None, summary="Sign in")
@messages(invalid=_email_or(DEFAULT_INVALID))
def sign_in(request: HttpRequest, body: SignInIn) -> dict[str, Any]:
    if not is_valid_email(body.email):
        raise ApiError(400, INVALID_EMAIL)
    limit = throttle.SIGNIN_FAILURES
    key = f"signin-fail:{normalize_email_address(body.email)}:{throttle.client_key(request)}"
    # Read before the password is checked: a key at its limit gets 429 without hashing, even for
    # the right password, so 429 versus 200 can never confirm a guess (§4.5).
    throttle.check(key, limit.limit, limit.window_s)
    # ModelBackend hashes once for an unknown email too (Better Auth's timing equalisation), and
    # re-hashes an imported ba_scrypt password with PBKDF2 on success.
    user = authenticate(request, username=body.email, password=body.password)
    if user is None:
        throttle.throttle(key, limit.limit, limit.window_s)
        raise ApiError(400, INVALID_CREDENTIALS)
    user = cast(User, user)
    _start_session(request, user)
    return _auth_out(user)


@auth.post("/sign-out", response=OkOut, summary="Sign out")
def sign_out(request: HttpRequest) -> dict[str, Any]:
    logout(request)
    return {"ok": True}


@auth.get("/config", response=ConfigOut, auth=None, summary="Auth config")
def auth_config(request: HttpRequest) -> dict[str, Any]:
    return {"signup_open": django_settings.ENV.signup_open}


# --- /me -----------------------------------------------------------------------------------------


@me.get("", response=MeOut, summary="Me")
def get_me(request: HttpRequest) -> dict[str, Any]:
    user = current_user(request)
    return {
        "id": user.pk,
        "name": user.name,
        "email": user.email,
        "signup_open": django_settings.ENV.signup_open,
    }


# --- /preferences and /settings ------------------------------------------------------------------


@preferences.put("", response=OkMessageOut, summary="Save preferences")
@messages(invalid=preferences_invalid)
def put_preferences(request: HttpRequest, body: Preferences) -> dict[str, Any]:
    profile.save_preferences(current_user(request).pk, body)
    return {"ok": True, "message": PREFERENCES_SAVED}


@settings.patch("", response=SettingsSaved, summary="Save settings")
@messages(invalid=SETTINGS_INVALID)
def patch_settings(request: HttpRequest, body: SettingsPatch) -> dict[str, Any]:
    # Only the keys that were sent: a partial save never resets a field to its default.
    sent = body.model_dump(include=body.model_fields_set)
    saved = profile.update_settings(current_user(request).pk, sent)
    return {"ok": True, "message": SETTINGS_SAVED, "settings": saved.model_dump()}
