"""An encrypted column must never store plaintext when a key is configured.

`EncryptedString` and `EncryptedJSON` back the columns that hold check targets
with embedded credentials, database connection strings and `check_config` API
keys. Returning the plaintext when `fernet.encrypt` fails would put a secret
in the database in the clear, under a column every reader believes is
encrypted, with a log line as the only trace.

These tests fail if such a fallback exists.
"""

from __future__ import annotations

import pytest
from app.core.config import settings
from app.core.encrypted_types import EncryptedJSON, EncryptedString
from app.core.exceptions import DatabaseException
from cryptography.fernet import Fernet


class _RefusingFernet:
    """A Fernet whose encrypt fails the way a wrong-length key or a corrupt
    keyring would — the case the fallback was written for."""

    def encrypt(self, _data: bytes) -> bytes:
        raise ValueError("cannot encrypt")


@pytest.fixture
def key() -> str:
    return Fernet.generate_key().decode()


class TestEncryptedString:
    def test_encryption_failure_raises_rather_than_storing_plaintext(
        self, monkeypatch, key
    ):
        col = EncryptedString()
        monkeypatch.setattr(col, "_get_fernet", lambda: _RefusingFernet())

        with pytest.raises(DatabaseException):
            col.process_bind_param("postgresql://user:hunter2@db/app", None)

    def test_a_configured_key_round_trips(self, monkeypatch, key):
        col = EncryptedString()
        monkeypatch.setattr(col, "_get_fernet", lambda: Fernet(key.encode()))

        stored = col.process_bind_param("hunter2", None)
        assert stored is not None
        assert "hunter2" not in stored
        assert col.process_result_value(stored, None) == "hunter2"

    def test_no_key_configured_still_stores_plaintext(self, monkeypatch):
        """The documented no-encryption mode. Distinct from a FAILURE to
        encrypt, and the reason the raise is scoped to the except branch."""
        col = EncryptedString()
        monkeypatch.setattr(col, "_get_fernet", lambda: None)
        assert col.process_bind_param("hunter2", None) == "hunter2"


def test_a_column_follows_the_key_configured_now(field_key, monkeypatch) -> None:
    """A type that has encrypted under one key encrypts under the next: the
    key is resolved at startup, after the models (and their types) exist."""
    col = EncryptedString()
    first = col.process_bind_param("hunter2", None)
    assert first is not None
    assert Fernet(field_key.encode()).decrypt(first.encode()) == b"hunter2"

    second_key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings.security, "field_encryption_key", second_key)
    second = col.process_bind_param("hunter2", None)
    assert second is not None
    assert Fernet(second_key.encode()).decrypt(second.encode()) == b"hunter2"

    monkeypatch.setattr(settings.security, "field_encryption_key", "")
    assert col.process_bind_param("hunter2", None) == "hunter2"


class TestEncryptedJSON:
    def test_encryption_failure_raises_rather_than_storing_plaintext(self, monkeypatch):
        col = EncryptedJSON()
        monkeypatch.setattr(col, "_get_fernet", lambda: _RefusingFernet())

        with pytest.raises(DatabaseException):
            col.process_bind_param({"api_key": "hunter2"}, None)

    def test_a_configured_key_round_trips(self, monkeypatch, key):
        col = EncryptedJSON()
        monkeypatch.setattr(col, "_get_fernet", lambda: Fernet(key.encode()))

        stored = col.process_bind_param({"api_key": "hunter2"}, None)
        assert stored is not None
        assert "hunter2" not in stored
        assert col.process_result_value(stored, None) == {"api_key": "hunter2"}


class TestEncryptedJSONReads:
    """What an EncryptedJSON column reads back: the object or array it holds."""

    def test_a_configured_key_round_trips_an_object(self, monkeypatch, key):
        col = EncryptedJSON()
        monkeypatch.setattr(col, "_get_fernet", lambda: Fernet(key.encode()))

        stored = col.process_bind_param({"api_key": "hunter2", "ports": [22]}, None)
        assert stored is not None and "hunter2" not in stored
        assert col.process_result_value(stored, None) == {
            "api_key": "hunter2",
            "ports": [22],
        }

    def test_a_legacy_plaintext_row_still_reads(self, monkeypatch, key):
        col = EncryptedJSON()
        monkeypatch.setattr(col, "_get_fernet", lambda: Fernet(key.encode()))

        assert col.process_result_value('{"timeout": 5}', None) == {"timeout": 5}

    @pytest.mark.parametrize("raw", ["not json", "42", '"text"'])
    def test_a_row_that_is_not_an_object_or_array_reads_as_none(
        self, monkeypatch, key, raw
    ):
        # The column's contract is a JSON object or array; a bare value was
        # previously cast and handed to callers that index it like a dict.
        col = EncryptedJSON()
        monkeypatch.setattr(col, "_get_fernet", lambda: Fernet(key.encode()))

        assert col.process_result_value(raw, None) is None
