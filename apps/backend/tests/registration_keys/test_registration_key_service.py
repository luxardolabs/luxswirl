"""Unit + integration tests for RegistrationKeyCoreService.

Focused coverage on the genuinely risky, previously-untested paths (LUXSWIRL-127):
the key crypto (generate/hash/verify) and the agent-registration auth path
(verify_key_and_update_usage) — including the security property that a REVOKED
key must not authenticate.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.registration_key_model import RegistrationKey
from app.schemas.registration_key_schema import RegistrationKeyCreate, RegistrationKeyRevoke
from app.services.core.registration_key_core_service import RegistrationKeyCoreService

svc = RegistrationKeyCoreService


# ---------------------------------------------------------------------------
# Key crypto — pure, no DB
# ---------------------------------------------------------------------------


class TestKeyCrypto:
    def test_generate_key_format(self):
        k = svc.generate_key()
        assert k.startswith("luxswirl_rk_")
        random_part = k.removeprefix("luxswirl_rk_")
        assert len(random_part) == 32
        int(random_part, 16)  # must be valid hex
        # 12-char prefix + 32 hex = 44 (the docstring's "42" is wrong).
        assert len(k) == 44

    def test_generate_key_unique(self):
        assert svc.generate_key() != svc.generate_key()

    def test_hash_verify_roundtrip(self):
        k = svc.generate_key()
        h = svc.hash_key(k)
        assert h != k  # stored hash is never the plaintext
        assert svc.verify_key(k, h) is True

    def test_verify_wrong_key_is_false(self):
        h = svc.hash_key(svc.generate_key())
        assert svc.verify_key(svc.generate_key(), h) is False

    def test_verify_malformed_hash_does_not_raise(self):
        # bcrypt.checkpw on a non-bcrypt hash raises; the service must swallow it
        # and return False rather than 500 the registration endpoint.
        assert svc.verify_key("luxswirl_rk_whatever", "not-a-bcrypt-hash") is False


# ---------------------------------------------------------------------------
# Agent-registration auth path — DB
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestVerifyAndUpdateUsage:
    @staticmethod
    async def _make(db, name="k"):
        return await svc.create_key(db, RegistrationKeyCreate(name=name))

    async def test_create_key_returns_verifiable_plaintext(self, db: AsyncSession):
        key, plaintext = await self._make(db, "prod")
        assert plaintext.startswith("luxswirl_rk_")
        assert key.key_hash != plaintext  # only the hash is persisted
        assert svc.verify_key(plaintext, key.key_hash) is True

    async def test_valid_key_authenticates_and_bumps_usage(self, db: AsyncSession):
        key, plaintext = await self._make(db, "valid")
        assert key.usage_count == 0
        got = await svc.verify_key_and_update_usage(db, plaintext)
        assert got is not None
        assert got.id == key.id
        assert got.usage_count == 1
        assert got.last_used_at is not None
        again = await svc.verify_key_and_update_usage(db, plaintext)
        assert again.usage_count == 2

    async def test_invalid_key_returns_none(self, db: AsyncSession):
        await self._make(db, "real")
        bogus = "luxswirl_rk_" + ("0" * 32)
        assert await svc.verify_key_and_update_usage(db, bogus) is None

    async def test_revoked_key_cannot_authenticate(self, db: AsyncSession):
        key, plaintext = await self._make(db, "revoked")
        await svc.revoke_key(db, key.id, RegistrationKeyRevoke(reason="test"))
        # SECURITY: a revoked key must NOT authenticate, even with the right plaintext.
        assert await svc.verify_key_and_update_usage(db, plaintext) is None

    async def test_corrupted_hash_does_not_break_verification(self, db: AsyncSession):
        # ADVERSARIAL: a key row with a non-bcrypt hash (corruption / bad migration)
        # must be skipped, not crash registration — and a valid key still works.
        good, plaintext = await self._make(db, "good")
        db.add(RegistrationKey(name="corrupt", key_hash="not-a-bcrypt-hash"))
        await db.flush()
        got = await svc.verify_key_and_update_usage(db, plaintext)
        assert got is not None
        assert got.id == good.id
