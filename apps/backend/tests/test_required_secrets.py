"""The field-encryption key is a required setting: an unset, empty or malformed
value refuses to start, and the `.env` example's value is refused."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.core.config import SecuritySettings
from cryptography.fernet import Fernet
from pydantic import ValidationError

ENV_EXAMPLE = Path(__file__).resolve().parents[3] / ".env.example"


def _example_value(name: str) -> str:
    for line in ENV_EXAMPLE.read_text().splitlines():
        if line.startswith(f"{name}="):
            return line.partition("=")[2]
    raise AssertionError(f"{name} is not in .env.example")


def test_a_fernet_key_is_accepted() -> None:
    key = Fernet.generate_key().decode()

    assert SecuritySettings(field_encryption_key=key).field_encryption_key == key


def test_an_unset_key_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SECURITY__FIELD_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("SECURITY_FIELD_ENCRYPTION_KEY", raising=False)

    with pytest.raises(ValidationError, match="field_encryption_key"):
        SecuritySettings(_env_file=None)


@pytest.mark.parametrize(
    "key", ["", "x" * 44, "not-a-fernet-key-but-long-enough-to-pass-32"]
)
def test_an_empty_or_malformed_key_is_refused(key: str) -> None:
    with pytest.raises(ValidationError, match="field_encryption_key"):
        SecuritySettings(field_encryption_key=key)


def test_the_env_example_value_is_refused() -> None:
    with pytest.raises(ValidationError, match="field_encryption_key"):
        SecuritySettings(
            field_encryption_key=_example_value("SECURITY__FIELD_ENCRYPTION_KEY")
        )


def test_a_refused_value_is_not_in_the_error() -> None:
    almost_key = Fernet.generate_key().decode()[:-2]

    with pytest.raises(ValidationError) as refused:
        SecuritySettings(field_encryption_key=almost_key)

    assert almost_key not in str(refused.value)


def test_a_retired_secret_key_setting_does_not_stop_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECURITY_SECRET_KEY", "left-over-from-an-older-env-file")

    SecuritySettings(field_encryption_key=Fernet.generate_key().decode())
