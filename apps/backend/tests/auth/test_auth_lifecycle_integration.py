"""Integration tests for the auth lifecycle: authenticate → session → logout.

Covers the stateful pieces of AuthCoreService that talk to the DB:
- authenticate_user (success path, wrong password, locked, inactive, missing)
- create_session / verify_session / logout
- failed-attempt counter + account lock
- session expiry handling
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest
from app.core.datetime_utils import utc_now
from app.crud.session_crud import SessionCRUD
from app.models.session_model import Session
from app.services.core.auth_core_service import AuthCoreService
from fixtures.factories import make_user
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def _stored(db: AsyncSession, session_id: UUID) -> Session:
    """The persisted session row: create_session hands back only a DTO."""
    row = await db.get(Session, session_id)
    assert row is not None
    return row


@pytest.fixture
def auth():
    return AuthCoreService()


# ---------------------------------------------------------------------------
# authenticate_user
# ---------------------------------------------------------------------------


class TestAuthenticateUser:
    async def test_correct_password_returns_user(self, db: AsyncSession, auth):
        # make_user defaults to password "TestPass123!"
        user = make_user(username="alice")
        db.add(user)
        await db.flush()

        result = await auth.authenticate_user(db, "alice", "TestPass123!")
        assert result is not None
        assert result.id == user.id

    async def test_wrong_password_returns_none(self, db: AsyncSession, auth):
        user = make_user(username="bob")
        db.add(user)
        await db.flush()

        assert await auth.authenticate_user(db, "bob", "wrong") is None

    async def test_unknown_user_returns_none(self, db: AsyncSession, auth):
        # No user exists — should return None without raising
        result = await auth.authenticate_user(db, "ghost", "anything")
        assert result is None

    async def test_inactive_user_returns_none(self, db: AsyncSession, auth):
        user = make_user(username="dormant", is_active=False)
        db.add(user)
        await db.flush()

        # Even with the correct password, an inactive user can't log in
        assert (await auth.authenticate_user(db, "dormant", "TestPass123!")) is None

    async def test_locked_user_returns_none(self, db: AsyncSession, auth):
        user = make_user(
            username="locked",
            locked_until=utc_now() + timedelta(hours=1),
        )
        db.add(user)
        await db.flush()

        assert (await auth.authenticate_user(db, "locked", "TestPass123!")) is None

    async def test_failed_attempts_increment(self, db: AsyncSession, auth):
        user = make_user(username="fumbler")
        db.add(user)
        await db.flush()
        user_id = user.id

        await auth.authenticate_user(db, "fumbler", "wrong-1")
        await db.flush()
        await auth.authenticate_user(db, "fumbler", "wrong-2")
        await db.flush()
        # Re-load fresh from DB to be sure we're seeing persisted state
        await db.refresh(user)
        # The audit-log path on a wrong password increments the counter; both
        # attempts should be recorded.
        assert user.failed_login_attempts == 2, (
            f"expected 2 failed attempts, got {user.failed_login_attempts} (user_id={user_id})"
        )

    async def test_account_locks_after_max_failed_attempts(
        self, db: AsyncSession, auth
    ):
        """5th failed attempt locks the account (per default
        security.max_failed_attempts)."""
        user = make_user(username="attacker")
        db.add(user)
        await db.flush()

        # 5 failed attempts → account locked
        for _ in range(5):
            await auth.authenticate_user(db, "attacker", "wrong")
            await db.flush()
        await db.refresh(user)
        assert user.locked_until is not None
        assert user.locked_until > utc_now()

        # Even correct password is rejected now
        assert (await auth.authenticate_user(db, "attacker", "TestPass123!")) is None

    async def test_successful_login_resets_failed_counter(self, db: AsyncSession, auth):
        user = make_user(username="redeemed", failed_login_attempts=2)
        db.add(user)
        await db.flush()

        await auth.authenticate_user(db, "redeemed", "TestPass123!")
        await db.flush()
        await db.refresh(user)
        assert user.failed_login_attempts == 0
        assert user.last_login_at is not None


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


class TestSessionLifecycle:
    async def test_create_session_returns_token_and_persists(
        self, db: AsyncSession, auth
    ):
        user = make_user()
        db.add(user)
        await db.flush()

        created, token = await auth.create_session(
            db, user.id, ip_address="10.0.0.1", user_agent="test-agent"
        )
        session = await _stored(db, created.id)
        # Token is the hex-encoded random value the client gets
        assert len(token) == 64
        # Session row stores only the hash, not the plaintext
        assert session.token_hash == AuthCoreService.hash_token(token)
        assert session.token_hash != token
        assert session.user_id == user.id
        assert session.ip_address == "10.0.0.1"
        assert session.expires_at > utc_now()

    async def test_verify_session_with_valid_token(self, db: AsyncSession, auth):
        user = make_user()
        db.add(user)
        await db.flush()
        _, token = await auth.create_session(db, user.id)

        loaded = await auth.verify_session(db, token)
        assert loaded is not None
        assert loaded.user_id == user.id

    async def test_verify_session_with_bogus_token_returns_none(
        self, db: AsyncSession, auth
    ):
        assert await auth.verify_session(db, "not-a-real-token") is None

    async def test_verify_session_expired_returns_none(self, db: AsyncSession, auth):
        user = make_user()
        db.add(user)
        await db.flush()
        created, token = await auth.create_session(db, user.id)
        session = await _stored(db, created.id)

        # Backdate the session past expiry
        session.expires_at = utc_now() - timedelta(minutes=1)
        await db.flush()

        assert await auth.verify_session(db, token) is None

    async def test_logout_destroys_session(self, db: AsyncSession, auth):
        user = make_user()
        db.add(user)
        await db.flush()
        _, token = await auth.create_session(db, user.id)

        ok = await auth.logout(db, token)
        assert ok is True
        # The request's commit flushes; the app's sessions do not autoflush.
        await db.flush()
        # Session is gone — verify returns None
        assert await auth.verify_session(db, token) is None

    async def test_logout_unknown_token_returns_false(self, db: AsyncSession, auth):
        assert await auth.logout(db, "nonexistent-token") is False


# ---------------------------------------------------------------------------
# Multi-session helpers
# ---------------------------------------------------------------------------


class TestMultiSessionHelpers:
    async def test_get_user_sessions(self, db: AsyncSession, auth):
        user = make_user()
        db.add(user)
        await db.flush()
        for _ in range(3):
            await auth.create_session(db, user.id)

        sessions = await auth.get_user_sessions(db, user.id)
        assert len(sessions) == 3

    async def test_logout_all_sessions(self, db: AsyncSession, auth):
        user = make_user()
        db.add(user)
        await db.flush()
        for _ in range(3):
            await auth.create_session(db, user.id)

        deleted = await auth.logout_all_sessions(db, user.id)
        assert deleted == 3
        # All gone
        assert await auth.get_user_sessions(db, user.id) == []

    async def test_logout_all_leaves_other_users_sessions(self, db: AsyncSession, auth):
        user = make_user(username="leaver")
        other = make_user(username="stayer")
        db.add_all([user, other])
        await db.flush()
        await auth.create_session(db, user.id)
        await auth.create_session(db, other.id)

        assert await auth.logout_all_sessions(db, user.id) == 1
        assert len(await auth.get_user_sessions(db, other.id)) == 1


# ---------------------------------------------------------------------------
# cleanup_expired_sessions
# ---------------------------------------------------------------------------


class TestCleanupExpiredSessions:
    async def test_removes_only_expired(self, db: AsyncSession, auth):
        user = make_user()
        db.add(user)
        await db.flush()
        # 2 expired, 1 fresh
        fresh, _ = await auth.create_session(db, user.id)
        expired_a, _ = await auth.create_session(db, user.id)
        expired_b, _ = await auth.create_session(db, user.id)
        (await _stored(db, expired_a.id)).expires_at = utc_now() - timedelta(hours=1)
        (await _stored(db, expired_b.id)).expires_at = utc_now() - timedelta(hours=2)
        await db.flush()

        count = await auth.cleanup_expired_sessions(db)
        assert count == 2

        remaining = await auth.get_user_sessions(db, user.id)
        assert len(remaining) == 1
        assert remaining[0].id == fresh.id


# ---------------------------------------------------------------------------
# change_password
# ---------------------------------------------------------------------------


class TestChangePassword:
    async def test_correct_old_password_rotates_hash(self, db: AsyncSession, auth):
        user = make_user(username="rotator")
        db.add(user)
        await db.flush()
        original_hash = user.password_hash

        await auth.change_password(db, user.id, "TestPass123!", "NewPassword456!")
        # Flush before refresh: refresh reloads from the database, and the app's
        # sessions do not autoflush, so an unflushed change would be discarded.
        await db.flush()
        await db.refresh(user)
        assert user.password_hash != original_hash
        # New password works
        assert AuthCoreService.verify_password("NewPassword456!", user.password_hash)
        # Old password no longer works
        assert not AuthCoreService.verify_password("TestPass123!", user.password_hash)

    async def test_wrong_old_password_raises(self, db: AsyncSession, auth):
        user = make_user(username="hacker-target")
        db.add(user)
        await db.flush()

        with pytest.raises(ValueError, match="Current password is incorrect"):
            await auth.change_password(db, user.id, "wrong-old", "NewPassword456!")

    async def test_password_change_invalidates_existing_sessions(
        self, db: AsyncSession, auth
    ):
        """Security: changing the password destroys all live sessions so a
        stolen session can't survive a password rotation."""
        user = make_user(username="rotator-with-sessions")
        db.add(user)
        await db.flush()
        for _ in range(3):
            await auth.create_session(db, user.id)
        assert len(await auth.get_user_sessions(db, user.id)) == 3

        await auth.change_password(db, user.id, "TestPass123!", "NewPassword456!")
        assert await auth.get_user_sessions(db, user.id) == []


class TestDeleteExpiredBefore:
    async def test_counts_and_removes_only_sessions_expired_before_the_cutoff(
        self, db: AsyncSession, auth
    ):
        user = make_user(username="aged")
        db.add(user)
        await db.flush()
        old, _ = await auth.create_session(db, user.id)
        recent, _ = await auth.create_session(db, user.id)
        (await _stored(db, old.id)).expires_at = utc_now() - timedelta(days=10)
        (await _stored(db, recent.id)).expires_at = utc_now() - timedelta(days=1)
        await db.flush()

        deleted = await SessionCRUD.delete_expired_before(
            db, utc_now() - timedelta(days=7)
        )

        assert deleted == 1
        assert await db.get(Session, old.id) is None
        assert await db.get(Session, recent.id) is not None
