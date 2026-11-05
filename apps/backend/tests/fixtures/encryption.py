"""A fresh field-encryption key for one test.

The suite runs under its own key (SECURITY__FIELD_ENCRYPTION_KEY in the test
environment). The encrypted column types read the configured key on every use,
so setting another for one test encrypts that test's writes under it alone.
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
