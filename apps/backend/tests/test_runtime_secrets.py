"""Runtime secrets resolve env → file → generate, and a generated one survives a restart.

A secret that changed on every boot would sign every session and agent out at
each restart, and one written world-readable would leak through the data
volume. These pin the order of resolution, the persistence, and the file mode.
"""

from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from app.core import secrets as runtime_secrets
from cryptography.fernet import Fernet


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_an_env_value_wins_and_writes_nothing(tmp_path: Path) -> None:
    value, generated = runtime_secrets.load_or_generate(
        "secret_key", "from-env", secrets_dir=tmp_path
    )

    assert (value, generated) == ("from-env", False)
    assert not (tmp_path / "secret_key").exists()


def test_a_persisted_value_is_reused(tmp_path: Path) -> None:
    (tmp_path / "secret_key").write_text("persisted\n")

    value, generated = runtime_secrets.load_or_generate(
        "secret_key", None, secrets_dir=tmp_path
    )

    assert (value, generated) == ("persisted", False)


def test_a_generated_value_is_persisted_owner_only_and_stable(tmp_path: Path) -> None:
    first, generated = runtime_secrets.load_or_generate(
        "secret_key", None, secrets_dir=tmp_path
    )
    again, regenerated = runtime_secrets.load_or_generate(
        "secret_key", None, secrets_dir=tmp_path
    )

    assert generated is True and regenerated is False
    assert first == again and len(first) >= 64
    assert _mode(tmp_path / "secret_key") == 0o600


def test_an_empty_file_counts_as_no_secret(tmp_path: Path) -> None:
    (tmp_path / "secret_key").write_text("  \n")

    value, generated = runtime_secrets.load_or_generate(
        "secret_key", None, secrets_dir=tmp_path
    )

    assert generated is True and value.strip()


def test_a_missing_directory_is_created(tmp_path: Path) -> None:
    nested = tmp_path / "data" / "server"

    runtime_secrets.load_or_generate("secret_key", None, secrets_dir=nested)

    assert (nested / "secret_key").exists()


def test_a_custom_generator_shapes_the_secret(tmp_path: Path) -> None:
    value, _ = runtime_secrets.load_or_generate(
        "field_encryption_key",
        None,
        generator=lambda: "custom-shape",
        secrets_dir=tmp_path,
    )

    assert value == "custom-shape"


def _settings(**security: object) -> Any:
    """Just the `security` block `resolve_runtime_secrets` reads and writes."""
    defaults: dict[str, object] = {
        "secret_key": "",
        "field_encryption_key": "",
    }
    return SimpleNamespace(security=SimpleNamespace(**(defaults | security)))


def test_resolution_fills_every_secret_and_keeps_them_across_boots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_secrets, "SECRETS_DIR", tmp_path)
    first = _settings()
    runtime_secrets.resolve_runtime_secrets(first)
    second = _settings()
    runtime_secrets.resolve_runtime_secrets(second)

    assert first.security.secret_key and first.security.field_encryption_key
    assert vars(first.security) == vars(second.security)
    Fernet(first.security.field_encryption_key.encode())  # a usable Fernet key
    for name in ("secret_key", "field_encryption_key"):
        assert _mode(tmp_path / name) == 0o600


def test_configured_values_are_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_secrets, "SECRETS_DIR", tmp_path)
    key = Fernet.generate_key().decode()
    settings = _settings(secret_key="configured", field_encryption_key=key)

    runtime_secrets.resolve_runtime_secrets(settings)

    assert settings.security.secret_key == "configured"
    assert settings.security.field_encryption_key == key
    assert list(tmp_path.iterdir()) == []


def test_no_shared_api_token_is_generated_or_announced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The JSON API is the agent protocol, authenticated by agent and
    registration keys: no shared admin token is minted, stored or printed."""
    monkeypatch.setattr(runtime_secrets, "SECRETS_DIR", tmp_path)
    settings = _settings()

    runtime_secrets.resolve_runtime_secrets(settings)

    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "field_encryption_key",
        "secret_key",
    ]
    assert set(vars(settings.security)) == {"secret_key", "field_encryption_key"}
    assert "API TOKEN" not in caplog.text
