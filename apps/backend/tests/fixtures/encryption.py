"""A field-encryption key for one test, and none for the next.

The suite runs with no key, so encrypted columns store plaintext. The encrypted
column types read the configured key on every use, so setting it for one test
encrypts that test's writes and nothing after it.
"""

from __future__ import annotations

import pytest
from app.core.config import settings
from cryptography.fernet import Fernet


@pytest.fixture
def field_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """Encrypt every encrypted column under a fresh key for this test; the key."""
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings.security, "field_encryption_key", key)
    return key
