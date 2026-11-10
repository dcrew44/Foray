"""Mail tokens at rest (#26): `apps.mail.crypto`, and the two variables that turn Gmail on."""

from __future__ import annotations

import base64
from typing import Any

import pytest
from cryptography.fernet import Fernet, InvalidToken

from apps.mail import crypto
from foray.env import ConfigurationError, build_env

TOKEN = "1//0g-a-made-up-refresh-token-that-is-not-real"


def test_a_sealed_token_is_not_the_token_and_opens_again() -> None:
    sealed = crypto.seal(TOKEN)
    assert sealed.startswith("v1:") and TOKEN not in sealed
    assert crypto.unseal(sealed) == TOKEN
    # Sealing twice gives two values, so equal tokens cannot be spotted in the table.
    assert crypto.seal(TOKEN) != sealed
    # Not an encoding of it either.
    assert base64.b64encode(TOKEN.encode()).decode() not in sealed
    assert base64.urlsafe_b64encode(TOKEN.encode()).decode().rstrip("=") not in sealed


def test_the_key_is_derived_from_secret_key_and_is_not_secret_key(settings: Any) -> None:
    sealed = crypto.seal(TOKEN)
    token = sealed.removeprefix("v1:").encode()
    # `SECRET_KEY` itself, used as a key the obvious way, does not open it.
    naive = base64.urlsafe_b64encode(settings.SECRET_KEY.encode()[:32])
    with pytest.raises(InvalidToken):
        Fernet(naive).decrypt(token)

    # Another install's key (or this one's, after it was changed) cannot open it.
    settings.SECRET_KEY = "another-installs-secret-key-0123456789-0123456789-01234"
    with pytest.raises(crypto.Unreadable):
        crypto.unseal(sealed)
    theirs = crypto.seal(TOKEN)
    assert crypto.unseal(theirs) == TOKEN
    settings.SECRET_KEY = "test-secret-key-0123456789-0123456789-0123456789-a"
    assert crypto.unseal(sealed) == TOKEN
    with pytest.raises(crypto.Unreadable):
        crypto.unseal(theirs)


@pytest.mark.parametrize(
    "stored",
    ["", "v1:", "v1:not-a-token", "v2:whatever", TOKEN, "v1:" + "A" * 120, "v1:éé"],
)
def test_anything_else_is_unreadable_and_says_nothing(stored: str) -> None:
    with pytest.raises(crypto.Unreadable) as refused:
        crypto.unseal(stored)
    assert str(refused.value) == "" and refused.value.__cause__ is None


def test_a_tampered_token_is_refused() -> None:
    sealed = crypto.seal(TOKEN)
    flipped = sealed[:-3] + ("A" if sealed[-3] != "A" else "B") + sealed[-2:]
    with pytest.raises(crypto.Unreadable):
        crypto.unseal(flipped)


# --- GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET ---------------------------------------------------


def test_gmail_is_off_until_both_variables_are_set(monkeypatch: pytest.MonkeyPatch) -> None:
    env = build_env()
    assert (env.google_client_id, env.google_client_secret, env.mail_configured) == (
        None,
        None,
        False,
    )

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "an-id.apps.googleusercontent.example")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "a-made-up-secret")
    env = build_env()
    assert env.mail_configured is True
    assert env.google_client_secret is not None
    # The secret is a `SecretStr`: printing the settings does not print it.
    assert "a-made-up-secret" not in repr(env) and "a-made-up-secret" not in str(env)

    # An empty value is unset, like every other variable.
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "")
    assert build_env().mail_configured is False


@pytest.mark.parametrize(
    ("name", "missing"),
    [("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"), ("GOOGLE_CLIENT_SECRET", "GOOGLE_CLIENT_ID")],
)
def test_half_a_google_client_is_refused_at_boot(
    name: str, missing: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(name, "a-made-up-value")
    with pytest.raises(ConfigurationError) as refused:
        build_env()
    [line] = refused.value.lines
    assert line.startswith(f"{missing} is not set") and "docs/gmail.md" in line
    assert "a-made-up-value" not in line  # values are never printed
