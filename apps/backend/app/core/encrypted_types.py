"""
Custom SQLAlchemy types for field-level encryption.

Provides transparent encryption/decryption for sensitive database fields.
"""

import json
from typing import cast

from cryptography.fernet import Fernet, InvalidToken
from shared.logger import get_logger
from sqlalchemy import String, Text, TypeDecorator

from app.core.config import settings

logger = get_logger("luxswirl.models.encrypted")


class EncryptedString(TypeDecorator):
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

    def __init__(self, length: int = 500, **kwargs):
        """
        Initialize encrypted string type.

        Args:
            length: Maximum length of encrypted data (default 500)
                   Note: Encrypted data is ~50% larger than plaintext
            **kwargs: Additional SQLAlchemy type arguments
        """
        super().__init__(length, **kwargs)
        self._fernet: Fernet | None = None

    def _get_fernet(self) -> Fernet | None:
        """Get Fernet cipher instance (lazy initialization)."""
        if self._fernet is None:
            key = settings.security.field_encryption_key
            if not key or key.strip() == "":
                # No encryption key - return None to skip encryption
                return None
            self._fernet = Fernet(key.encode())
        return self._fernet

    def process_bind_param(self, value: str | None, dialect) -> str | None:
        """
        Encrypt value before saving to database.

        Args:
            value: Plaintext string to encrypt
            dialect: SQLAlchemy dialect (unused)

        Returns:
            Base64-encoded encrypted string, or plaintext if no encryption key
        """
        if value is None:
            return None

        fernet = self._get_fernet()
        if fernet is None:
            # No encryption key configured - store as plaintext
            return value

        try:
            encrypted = fernet.encrypt(value.encode())
            return cast(str, encrypted.decode())  # Store as base64 string
        except Exception:
            # Log error but don't crash - store plaintext as fallback
            logger.error("Failed to encrypt field", exc_info=True)
            return value

    def process_result_value(self, value: str | None, dialect) -> str | None:
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
        if fernet is None:
            # No encryption key configured - return as-is
            return value

        try:
            # Try to decrypt (will fail if plaintext)
            decrypted = fernet.decrypt(value.encode())
            return cast(str, decrypted.decode())
        except InvalidToken:
            # Not encrypted - return plaintext as-is
            # This handles migration from plaintext to encrypted
            return value
        except Exception:
            # Other error - log and return as-is
            logger.error("Failed to decrypt field", exc_info=True)
            return value


class EncryptedJSON(TypeDecorator):
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

    def __init__(self, **kwargs):
        """Initialize encrypted JSON type."""
        super().__init__(**kwargs)
        self._fernet: Fernet | None = None

    def _get_fernet(self) -> Fernet | None:
        """Get Fernet cipher instance (lazy initialization)."""
        if self._fernet is None:
            key = settings.security.field_encryption_key
            if not key or key.strip() == "":
                return None
            self._fernet = Fernet(key.encode())
        return self._fernet

    def process_bind_param(self, value: dict | list | None, dialect) -> str | None:
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
        if fernet is None:
            # No encryption key - store as JSON
            return json.dumps(value)

        try:
            # Serialize to JSON then encrypt
            json_str = json.dumps(value)
            encrypted = fernet.encrypt(json_str.encode())
            return cast(str, encrypted.decode())
        except Exception:
            logger.error("Failed to encrypt JSON field", exc_info=True)
            return json.dumps(value)

    def process_result_value(self, value: str | None, dialect) -> dict | list | None:
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
        if fernet is None:
            # No encryption key - parse as JSON
            try:
                return cast(dict | list, json.loads(value))
            except json.JSONDecodeError:
                return None

        try:
            # Try to decrypt (will fail if plaintext JSON)
            decrypted = fernet.decrypt(value.encode())
            return cast(dict | list, json.loads(decrypted.decode()))
        except InvalidToken:
            # Not encrypted - parse as plaintext JSON (migration support)
            try:
                return cast(dict | list, json.loads(value))
            except json.JSONDecodeError:
                return None
        except Exception:
            logger.error("Failed to decrypt JSON field", exc_info=True)
            return None
