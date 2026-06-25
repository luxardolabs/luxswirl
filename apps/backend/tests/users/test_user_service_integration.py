"""Integration tests for UserCoreService.

CRUD lifecycle, role enforcement, lockout/unlock, password complexity gate.
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

_tests_root = str(Path(__file__).resolve().parent.parent)
if _tests_root not in sys.path:
    sys.path.insert(0, _tests_root)

from fixtures.factories import make_user  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.schemas.user_schema import UserCreate, UserUpdate  # noqa: E402
from app.services.core.user_core_service import UserCoreService  # noqa: E402

pytestmark = pytest.mark.integration


@pytest.fixture
def svc():
    return UserCoreService()


def _create_data(**overrides) -> UserCreate:
    """Default UserCreate payload with a complexity-compliant password."""
    defaults = {
        "username": f"u-{uuid4().hex[:8]}",
        "password": "ComplexPass123!",
        "role": "viewer",
        "full_name": "Test User",
        "is_active": True,
    }
    defaults.update(overrides)
    return UserCreate(**defaults)


# ---------------------------------------------------------------------------
# create_user
# ---------------------------------------------------------------------------


class TestCreateUser:
    async def test_creates_user_with_hashed_password(self, db: AsyncSession, svc):
        u = await svc.create_user(db, _create_data(username="alice"))
        assert u.username == "alice"
        assert u.password_hash != "ComplexPass123!"  # hashed, not plaintext
        assert u.role == "viewer"

    async def test_duplicate_username_raises(self, db: AsyncSession, svc):
        await svc.create_user(db, _create_data(username="dup"))
        with pytest.raises(ValueError, match="already exists"):
            await svc.create_user(db, _create_data(username="dup"))

    async def test_weak_password_rejected(self, db: AsyncSession, svc):
        with pytest.raises(ValueError):
            await svc.create_user(
                db,
                _create_data(password="short"),
            )

    async def test_created_by_recorded(self, db: AsyncSession, svc):
        u = await svc.create_user(
            db,
            _create_data(username="audited"),
            created_by="admin-alice",
        )
        assert u.created_by == "admin-alice"


# ---------------------------------------------------------------------------
# get / list
# ---------------------------------------------------------------------------


class TestGetters:
    async def test_get_by_id(self, db: AsyncSession, svc):
        u = make_user()
        db.add(u)
        await db.flush()
        loaded = await svc.get_user_by_id(db, u.id)
        assert loaded.id == u.id

    async def test_get_by_id_missing(self, db: AsyncSession, svc):
        assert await svc.get_user_by_id(db, uuid4()) is None

    async def test_get_by_username(self, db: AsyncSession, svc):
        u = make_user(username="findable")
        db.add(u)
        await db.flush()
        loaded = await svc.get_user_by_username(db, "findable")
        assert loaded.id == u.id


# ---------------------------------------------------------------------------
# update_user
# ---------------------------------------------------------------------------


class TestUpdateUser:
    async def test_updates_role_and_full_name(self, db: AsyncSession, svc):
        u = await svc.create_user(db, _create_data(role="viewer"))
        updated = await svc.update_user(
            db,
            u.id,
            UserUpdate(role="editor", full_name="Renamed Person"),
        )
        assert updated.role == "editor"
        assert updated.full_name == "Renamed Person"

    async def test_partial_update_preserves_others(self, db: AsyncSession, svc):
        u = await svc.create_user(
            db,
            _create_data(role="admin", full_name="Original"),
        )
        updated = await svc.update_user(
            db,
            u.id,
            UserUpdate(full_name="Just the name"),
        )
        assert updated.role == "admin"  # untouched
        assert updated.full_name == "Just the name"

    async def test_missing_user_raises(self, db: AsyncSession, svc):
        with pytest.raises(Exception):  # noqa: B017, PT011
            await svc.update_user(db, uuid4(), UserUpdate(role="admin"))


# ---------------------------------------------------------------------------
# delete_user + unlock_user
# ---------------------------------------------------------------------------


class TestDeleteAndUnlock:
    async def test_delete_user(self, db: AsyncSession, svc):
        u = await svc.create_user(db, _create_data())
        ok = await svc.delete_user(db, u.id)
        assert ok is True
        assert await svc.get_user_by_id(db, u.id) is None

    async def test_delete_missing_raises(self, db: AsyncSession, svc):
        with pytest.raises(ValueError, match="not found"):
            await svc.delete_user(db, uuid4())

    async def test_unlock_clears_locked_until(self, db: AsyncSession, svc):
        from datetime import timedelta

        from app.core.datetime_utils import utc_now

        u = make_user(locked_until=utc_now() + timedelta(hours=1))
        db.add(u)
        await db.flush()
        assert u.is_locked is True

        unlocked = await svc.unlock_user(db, u.id)
        assert unlocked.locked_until is None
        assert unlocked.is_locked is False


# ---------------------------------------------------------------------------
# Stats + listing
# ---------------------------------------------------------------------------


class TestStatsAndListing:
    async def test_get_user_stats(self, db: AsyncSession, svc):
        # Mix of roles and active flags
        for role in ("admin", "admin", "editor", "viewer", "viewer", "viewer"):
            db.add(make_user(role=role, is_active=True))
        db.add(make_user(role="viewer", is_active=False))
        await db.flush()

        stats = await svc.get_user_stats(db)
        # UserStatsResponse: assert reasonable structure (impl-defined keys)
        assert stats.total_users == 7
        # Active users excludes is_active=False
        assert stats.active_users == 6

    async def test_get_active_user_count(self, db: AsyncSession):
        db.add(make_user(is_active=True))
        db.add(make_user(is_active=True))
        db.add(make_user(is_active=False))
        await db.flush()
        # Module-level static helper, no svc instance needed
        n = await UserCoreService.get_active_user_count(db)
        assert n == 2

    async def test_list_users_paginated(self, db: AsyncSession, svc):
        for i in range(7):
            db.add(make_user(username=f"list-user-{i:02d}"))
        await db.flush()
        rows, total = await svc.list_users(db, skip=0, limit=3)
        assert total == 7
        assert len(rows) == 3


# ---------------------------------------------------------------------------
# ensure_default_admin
# ---------------------------------------------------------------------------


class TestEnsureDefaultAdmin:
    """`ensure_default_admin`: seed from INITIAL_ADMIN config when none exists,
    defer to the /setup wizard when no password is configured, and no-op when an
    admin already exists."""

    async def test_creates_admin_from_config(self, db: AsyncSession, svc, monkeypatch):
        monkeypatch.setattr(settings.security, "initial_admin_username", "admin")
        monkeypatch.setattr(settings.security, "initial_admin_password", "ComplexPass123!")
        admin = await svc.ensure_default_admin(db)
        assert admin is not None
        assert admin.role == "admin"
        assert admin.must_change_password is True

    async def test_defers_to_setup_when_no_password(self, db: AsyncSession, svc, monkeypatch):
        monkeypatch.setattr(settings.security, "initial_admin_password", "")
        assert await svc.ensure_default_admin(db) is None

    async def test_no_op_when_admin_exists(self, db: AsyncSession, svc):
        existing = make_user(role="admin")
        db.add(existing)
        await db.flush()
        result = await svc.ensure_default_admin(db)
        assert result is not None
        assert result.id == existing.id
