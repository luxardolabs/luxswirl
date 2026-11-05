"""The server's two secrets are required settings: an unset, empty, short or
malformed value refuses to start, and the `.env` example's value is refused."""

from __future__ import annotations

import string
from pathlib import Path

import pytest
from app.core.config import SecuritySettings
from cryptography.fernet import Fernet
from pydantic import ValidationError

ENV_EXAMPLE = Path(__file__).resolve().parents[3] / ".env.example"
# A valid value, built so no literal secret sits in the source.
GOOD_SECRET = string.ascii_letters[:16] * 2


def _example_value(name: str) -> str:
    for line in ENV_EXAMPLE.read_text().splitlines():
        if line.startswith(f"{name}="):
            return line.partition("=")[2]
    raise AssertionError(f"{name} is not in .env.example")


@pytest.fixture
def no_secrets_in_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "SECURITY__SECRET_KEY",
        "SECURITY__FIELD_ENCRYPTION_KEY",
        "SECURITY_SECRET_KEY",
        "SECURITY_FIELD_ENCRYPTION_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def test_valid_secrets_are_accepted() -> None:
    key = Fernet.generate_key().decode()

    security = SecuritySettings(secret_key=GOOD_SECRET, field_encryption_key=key)

    assert (security.secret_key, security.field_encryption_key) == (GOOD_SECRET, key)


@pytest.mark.usefixtures("no_secrets_in_env")
@pytest.mark.parametrize("missing", ["secret_key", "field_encryption_key"])
def test_an_unset_secret_refuses_to_start(missing: str) -> None:
    given = {
        "secret_key": GOOD_SECRET,
        "field_encryption_key": Fernet.generate_key().decode(),
    }
    del given[missing]

    with pytest.raises(ValidationError, match=missing):
        SecuritySettings(**given, _env_file=None)


@pytest.mark.parametrize("secret", ["", "x" * 31])
def test_an_empty_or_short_secret_key_is_refused(secret: str) -> None:
    with pytest.raises(ValidationError, match="secret_key"):
        SecuritySettings(
            secret_key=secret, field_encryption_key=Fernet.generate_key().decode()
        )


@pytest.mark.parametrize(
    "key", ["", "x" * 44, "not-a-fernet-key-but-long-enough-to-pass-32"]
)
def test_an_empty_or_malformed_field_key_is_refused(key: str) -> None:
    with pytest.raises(ValidationError, match="field_encryption_key"):
        SecuritySettings(secret_key=GOOD_SECRET, field_encryption_key=key)


def test_the_env_example_values_are_refused() -> None:
    with pytest.raises(ValidationError) as refused:
        SecuritySettings(
            secret_key=_example_value("SECURITY__SECRET_KEY"),
            field_encryption_key=_example_value("SECURITY__FIELD_ENCRYPTION_KEY"),
        )

    fields = {error["loc"][0] for error in refused.value.errors()}
    assert fields == {"secret_key", "field_encryption_key"}, fields


@pytest.mark.parametrize(
    "secret",
    [
        " " * 40,
        "\t" * 40,
        "changeme-changeme-changeme-changeme",
        "your-secret-key-min-32-chars-here!!",
        "abababababababababababababababab",
    ],
)
def test_a_blank_repetitive_or_placeholder_secret_key_is_refused(secret: str) -> None:
    with pytest.raises(ValidationError, match="secret_key"):
        SecuritySettings(
            secret_key=secret, field_encryption_key=Fernet.generate_key().decode()
        )


def test_a_refused_value_is_not_in_the_error() -> None:
    mistyped = GOOD_SECRET[:28]
    almost_key = Fernet.generate_key().decode()[:-2]

    with pytest.raises(ValidationError) as refused:
        SecuritySettings(secret_key=mistyped, field_encryption_key=almost_key)

    assert mistyped not in str(refused.value)
    assert almost_key not in str(refused.value)
