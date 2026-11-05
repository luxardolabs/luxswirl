"""An encrypted column never stores plaintext.

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

    def test_no_key_configured_refuses_to_write(self, monkeypatch):
        """The key is a required setting; code that cleared it writes nothing."""
        monkeypatch.setattr(settings.security, "field_encryption_key", "")

        with pytest.raises(DatabaseException):
            EncryptedString().process_bind_param("hunter2", None)


def test_a_column_follows_the_key_configured_now(field_key, monkeypatch) -> None:
    """A type that has encrypted under one key encrypts under the next: the
    type reads the configured key on every use."""
    col = EncryptedString()
    first = col.process_bind_param("hunter2", None)
    assert first is not None
    assert Fernet(field_key.encode()).decrypt(first.encode()) == b"hunter2"

    second_key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings.security, "field_encryption_key", second_key)
    second = col.process_bind_param("hunter2", None)
    assert second is not None
    assert Fernet(second_key.encode()).decrypt(second.encode()) == b"hunter2"


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


def test_a_value_encrypted_under_another_key_is_refused_not_passed_on(
    field_key, monkeypatch, caplog
) -> None:
    """A token the configured key cannot decrypt was stored under another key:
    handed on as plaintext, its ciphertext would become the check's target and
    the next save would lose it."""
    other = Fernet.generate_key().decode()
    string_token = Fernet(other.encode()).encrypt(b"postgresql://u:hunter2@db/app")
    json_token = Fernet(other.encode()).encrypt(b'{"api_key": "hunter2"}')

    with pytest.raises(DatabaseException):
        EncryptedString().process_result_value(string_token.decode(), None)
    with pytest.raises(DatabaseException):
        EncryptedJSON().process_result_value(json_token.decode(), None)
    assert "does not decrypt under the configured" in caplog.text


def test_plaintext_and_null_still_read_under_a_key(field_key) -> None:
    assert EncryptedString().process_result_value("https://example.com", None) == (
        "https://example.com"
    )
    assert EncryptedJSON().process_result_value('{"timeout": 5}', None) == {
        "timeout": 5
    }
    assert EncryptedString().process_result_value(None, None) is None


def test_plaintext_json_holding_a_token_string_reads(field_key) -> None:
    """A legacy JSON value that holds another system's Fernet token as a string
    is plaintext: only url-safe base64 alone is shaped as a token."""
    foreign = Fernet(Fernet.generate_key()).encrypt(b"keystone").decode()

    assert EncryptedJSON().process_result_value(f'["{foreign}"]', None) == [foreign]
