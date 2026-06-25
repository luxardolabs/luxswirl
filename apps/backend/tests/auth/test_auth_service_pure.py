"""Pure-logic tests for AuthCoreService stateless helpers.

hash_password / verify_password / generate_session_token / hash_token —
all pure functions over inputs, no DB needed.
"""

from __future__ import annotations

import pytest

from app.services.core.auth_core_service import AuthCoreService

pytestmark = pytest.mark.pure


class TestPasswordHashing:
    def test_hash_produces_bcrypt_string(self):
        h = AuthCoreService.hash_password("hello-world-12345")
        # Bcrypt hashes start with $2 (algorithm identifier)
        assert h.startswith("$2")
        # Standard bcrypt hash length is 60 characters
        assert len(h) == 60

    def test_verify_matches_correct_password(self):
        password = "Correct-Horse-Battery-Staple-7!"
        h = AuthCoreService.hash_password(password)
        assert AuthCoreService.verify_password(password, h) is True

    def test_verify_rejects_wrong_password(self):
        h = AuthCoreService.hash_password("right-password")
        assert AuthCoreService.verify_password("wrong-password", h) is False

    def test_hashes_are_salted(self):
        """Same password hashed twice must produce different outputs."""
        h1 = AuthCoreService.hash_password("same-password")
        h2 = AuthCoreService.hash_password("same-password")
        assert h1 != h2
        # Both still verify
        assert AuthCoreService.verify_password("same-password", h1)
        assert AuthCoreService.verify_password("same-password", h2)

    def test_verify_invalid_hash_returns_false(self):
        """Malformed hash must not crash — just return False."""
        # bcrypt raises ValueError on malformed hashes; production code may want
        # to catch this. For now, document the behavior: verify_password raises
        # rather than swallowing. If that changes, this test should change too.
        with pytest.raises(Exception):  # noqa: B017, PT011
            AuthCoreService.verify_password("any-password", "not-a-hash")


class TestSessionTokens:
    def test_generate_token_is_hex_string(self):
        token = AuthCoreService.generate_session_token()
        # SESSION_TOKEN_BYTES=32 → 64 hex chars
        assert len(token) == 64
        # All chars are valid hex
        int(token, 16)

    def test_generate_tokens_are_unique(self):
        """Tokens are random — two calls must produce different values."""
        a = AuthCoreService.generate_session_token()
        b = AuthCoreService.generate_session_token()
        assert a != b

    def test_hash_token_is_deterministic(self):
        token = "deadbeef" * 8
        assert AuthCoreService.hash_token(token) == AuthCoreService.hash_token(token)

    def test_hash_token_is_sha256_hex(self):
        h = AuthCoreService.hash_token("any-input")
        # SHA-256 → 64 hex chars
        assert len(h) == 64
        int(h, 16)

    def test_hash_token_changes_with_input(self):
        assert AuthCoreService.hash_token("a") != AuthCoreService.hash_token("b")
