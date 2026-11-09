"""
Custom SQLAlchemy types for field-level encryption.

Provides transparent encryption/decryption for sensitive database fields.
"""

import base64
import binascii
import json
import re
from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import String, Text, TypeDecorator
from sqlalchemy.engine import Dialect

from app.core.config import settings
from app.core.exceptions import DatabaseException
from app.shared.logger import get_logger

logger = get_logger("luxswirl.models.encrypted")

# What an EncryptedJSON column holds: a JSON object or array.
JsonContainer = dict[str, Any] | list[Any]


# A Fernet token: version byte 0x80, then a timestamp, IV, ciphertext and HMAC
# (57 bytes at the least), url-safe base64.
_FERNET_VERSION = 0x80
_FERNET_MIN_BYTES = 57
_FERNET_ALPHABET = re.compile(r"[A-Za-z0-9_-]+={0,2}")


def _is_fernet_token(value: str) -> bool:
    """Whether ``value`` is shaped as a Fernet token (a value some key encrypted).

    A token is url-safe base64 alone, with the surrounding whitespace Fernet's
    own decoder ignores: any other character (JSON's brackets and quotes) makes
    it plaintext, whatever a lenient decoder would read.
    """
    token = value.strip()
    if not _FERNET_ALPHABET.fullmatch(token):
        return False
    try:
        raw = base64.urlsafe_b64decode(token.encode())
    except ValueError, binascii.Error:
        return False
    return len(raw) >= _FERNET_MIN_BYTES and raw[0] == _FERNET_VERSION


def _refuse_wrong_key() -> None:
    """A stored token the configured key cannot decrypt: it was encrypted under
    another key. Reading it as plaintext would hand its ciphertext on as the
    value, and the next save would lose it, so the read fails loudly.

    Raises:
        DatabaseException: Always.
    """
    logger.error(
        "An encrypted field does not decrypt under the configured "
        "SECURITY__FIELD_ENCRYPTION_KEY: it was encrypted under another key"
    )
    raise DatabaseException(
        "An encrypted field does not decrypt under the configured key"
    )


@lru_cache(maxsize=4)
def _fernet_for(key: str) -> Fernet:
    """One Fernet per key, built once."""
    return Fernet(key.encode())


def _configured_fernet() -> Fernet:
    """The Fernet for the key configured now.

    Read on every use, never kept on the type: SQLAlchemy runs each column
    type through a per-dialect copy, so a cipher cached on an instance outlives
    the key it was built from.

    Raises:
        DatabaseException: No key is configured (the setting is required, so
            only code that cleared it reaches this): nothing is written or
            read in the clear.
    """
    key = settings.security.field_encryption_key
    if not key or key.strip() == "":
        raise DatabaseException("No field encryption key is configured")
    return _fernet_for(key)


def decrypts(stored: str) -> bool:
    """Whether the configured key decrypts ``stored``, an encrypted column's
    value as stored."""
    try:
        _configured_fernet().decrypt(stored.encode())
    except InvalidToken:
        return False
    return True


def is_token(stored: str) -> bool:
    """Whether ``stored`` is shaped as a token some key wrote. A value that is
    not was stored before encryption: it reads as it is, under any key."""
    return _is_fernet_token(stored)


def fernet_key_error(key: str) -> str | None:
    """Why ``key`` is not a Fernet key (32 url-safe base64 bytes); None when it is."""
    try:
        Fernet(key.encode())
    except ValueError, binascii.Error:
        return "it is not a Fernet key (32 url-safe base64-encoded bytes)"
    return None


def decrypt_with(key: str, stored: str) -> str | None:
    """The plaintext ``key`` reads from ``stored`` (an encrypted column's value as
    stored); None when ``key`` does not decrypt it."""
    try:
        return _fernet_for(key).decrypt(stored.encode()).decode()
    except InvalidToken:
        return None


def encrypt_for_storage(plaintext: str) -> str:
    """``plaintext`` encrypted under the configured key, as an encrypted column stores it.

    Raises:
        DatabaseException: No key is configured.
    """
    return _configured_fernet().encrypt(plaintext.encode()).decode()


def _json_container(raw: str) -> JsonContainer | None:
    """The JSON object or array in ``raw``; None for invalid JSON or a bare value."""
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict | list) else None


class EncryptedString(TypeDecorator[str]):
    """
    SQLAlchemy type for encrypted string fields.

    Automatically encrypts data before saving to database and decrypts when loading.
    Transparent to application code - use like a normal String field.

    Example:
        class Check(Base):
            connection_string: Mapped[str | None] = mapped_column(
                EncryptedString(500), nullable=True
            )

    Security:
        - Uses Fernet (AES-128-CBC + HMAC) for encryption
        - Key from SECURITY__FIELD_ENCRYPTION_KEY environment variable
        - Stores encrypted binary data as base64 string in database
    """

    impl = String
    cache_ok = True

    def __init__(self, length: int = 500, **kwargs: Any) -> None:
        """
        Initialize encrypted string type.

        Args:
            length: Maximum length of encrypted data (default 500)
                   Note: Encrypted data is ~50% larger than plaintext
            **kwargs: Additional SQLAlchemy type arguments
        """
        super().__init__(length, **kwargs)

    def _get_fernet(self) -> Fernet:
        """The cipher for the configured key."""
        return _configured_fernet()

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        """
        Encrypt value before saving to database.

        Args:
            value: Plaintext string to encrypt
            dialect: SQLAlchemy dialect (unused)

        Returns:
            Base64-encoded encrypted string
        """
        if value is None:
            return None

        fernet = self._get_fernet()

        # NO plaintext fallback. This branch is reached only when a key IS
        # configured and encryption FAILED — and the columns this type is used
        # on hold check targets with embedded credentials and database
        # connection strings. Returning `value` here wrote the secret to the
        # database in the clear, under a column every reader believes is
        # encrypted, with nothing but a log line to say so. Failing the write is
        # the honest outcome: the caller finds out, and no credential is
        # silently downgraded.
        try:
            encrypted = fernet.encrypt(value.encode())
        except Exception as e:
            logger.exception("Failed to encrypt field — refusing to store plaintext")
            raise DatabaseException(
                "Could not encrypt a sensitive field; the value was not saved"
            ) from e
        return encrypted.decode()  # Store as base64 string

    def process_result_value(self, value: str | None, dialect: Dialect) -> str | None:
        """
        Decrypt value after loading from database.

        Args:
            value: Encrypted string from database
            dialect: SQLAlchemy dialect (unused)

        Returns:
            Decrypted plaintext string, or value as-is if not encrypted
        """
        if value is None:
            return None

        fernet = self._get_fernet()

        try:
            # Try to decrypt (will fail if plaintext)
            decrypted = fernet.decrypt(value.encode())
            return decrypted.decode()
        except InvalidToken:
            # A value stored before encryption reads as it is; one encrypted
            # under another key is refused.
            if _is_fernet_token(value):
                _refuse_wrong_key()
            return value
        # swallowed-exceptions: a row we cannot decrypt still has to load —
        # failing the read would take out every page that touches the table,
        # and the error is recorded.
        except Exception:
            logger.exception("Failed to decrypt field")
            return value


class EncryptedJSON(TypeDecorator[JsonContainer]):
    """
    SQLAlchemy type for encrypted JSON/JSONB fields.

    Automatically encrypts JSON data before saving and decrypts when loading.
    Stores encrypted data as text in database.

    Example:
        class Check(Base):
            check_config: Mapped[dict | None] = mapped_column(
                EncryptedJSON, nullable=True
            )

    Security:
        - Uses Fernet (AES-128-CBC + HMAC) for encryption
        - Key from SECURITY__FIELD_ENCRYPTION_KEY environment variable
        - Entire JSON object is encrypted (no field-level queries possible)
    """

    impl = Text
    cache_ok = True

    def _get_fernet(self) -> Fernet:
        """The cipher for the configured key."""
        return _configured_fernet()

    def process_bind_param(
        self, value: JsonContainer | None, dialect: Dialect
    ) -> str | None:
        """
        Encrypt JSON before saving to database.

        Args:
            value: Python dict/list to encrypt
            dialect: SQLAlchemy dialect (unused)

        Returns:
            Base64-encoded encrypted JSON string
        """
        if value is None:
            return None

        fernet = self._get_fernet()

        # NO plaintext fallback — same reasoning as EncryptedString above. This
        # type backs `check_config`, which holds API keys and query credentials.
        try:
            json_str = json.dumps(value)
            encrypted = fernet.encrypt(json_str.encode())
        except Exception as e:
            logger.exception(
                "Failed to encrypt JSON field — refusing to store plaintext"
            )
            raise DatabaseException(
                "Could not encrypt a sensitive field; the value was not saved"
            ) from e
        return encrypted.decode()

    def process_result_value(
        self, value: str | None, dialect: Dialect
    ) -> JsonContainer | None:
        """
        Decrypt JSON after loading from database.

        Args:
            value: Encrypted string from database
            dialect: SQLAlchemy dialect (unused)

        Returns:
            Decrypted Python dict/list
        """
        if value is None:
            return None

        fernet = self._get_fernet()

        try:
            # Try to decrypt (will fail if plaintext JSON)
            decrypted = fernet.decrypt(value.encode())
        except InvalidToken:
            # JSON stored before encryption reads as it is; a value encrypted
            # under another key is refused.
            if _is_fernet_token(value):
                _refuse_wrong_key()
            return _json_container(value)
        # swallowed-exceptions: a row we cannot decrypt still has to load —
        # failing the read would take out every page that touches the table,
        # and the error is recorded.
        except Exception:
            logger.exception("Failed to decrypt JSON field")
            return None
        return _json_container(decrypted.decode())
