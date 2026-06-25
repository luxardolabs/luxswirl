"""Integration tests for SettingsCoreService."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

_tests_root = str(Path(__file__).resolve().parent.parent)
if _tests_root not in sys.path:
    sys.path.insert(0, _tests_root)


from app.services.core.settings_core_service import SettingsCoreService  # noqa: E402

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# get_setting (with fallback) + create_setting
# ---------------------------------------------------------------------------


class TestGetCreateSetting:
    async def test_missing_returns_default(self, db: AsyncSession):
        value = await SettingsCoreService.get_setting(db, "x.nonexistent", default=42)
        assert value == 42

    async def test_missing_default_none(self, db: AsyncSession):
        value = await SettingsCoreService.get_setting(db, "x.nonexistent")
        assert value is None

    async def test_create_then_get_int(self, db: AsyncSession):
        s = await SettingsCoreService.create_setting(
            db,
            key="test.int_value",
            category="system",
            value=120,
            display_name="Test Int",
        )
        assert s.id is not None
        loaded = await SettingsCoreService.get_setting(db, "test.int_value")
        assert loaded == 120

    async def test_create_then_get_string(self, db: AsyncSession):
        await SettingsCoreService.create_setting(
            db,
            key="test.str_value",
            category="system",
            value="hello",
            display_name="Test String",
        )
        assert await SettingsCoreService.get_setting(db, "test.str_value") == "hello"

    async def test_create_then_get_bool(self, db: AsyncSession):
        await SettingsCoreService.create_setting(
            db,
            key="test.bool_value",
            category="system",
            value=True,
            display_name="Test Bool",
        )
        assert await SettingsCoreService.get_setting(db, "test.bool_value") is True

    async def test_create_then_get_list(self, db: AsyncSession):
        await SettingsCoreService.create_setting(
            db,
            key="test.list_value",
            category="system",
            value=["a", "b", "c"],
            display_name="Test List",
        )
        assert await SettingsCoreService.get_setting(db, "test.list_value") == [
            "a",
            "b",
            "c",
        ]


# ---------------------------------------------------------------------------
# update_setting + reset_setting
# ---------------------------------------------------------------------------


class TestUpdateResetSetting:
    async def test_update_changes_value(self, db: AsyncSession):
        await SettingsCoreService.create_setting(
            db,
            key="test.update_me",
            category="system",
            value=100,
            display_name="Update Test",
        )
        await SettingsCoreService.update_setting(db, "test.update_me", 200)
        assert (await SettingsCoreService.get_setting(db, "test.update_me")) == 200

    async def test_reset_restores_default(self, db: AsyncSession):
        await SettingsCoreService.create_setting(
            db,
            key="test.reset_me",
            category="system",
            value=50,
            display_name="Reset Test",
        )
        await SettingsCoreService.update_setting(db, "test.reset_me", 999)
        await SettingsCoreService.reset_setting(db, "test.reset_me")
        assert (await SettingsCoreService.get_setting(db, "test.reset_me")) == 50


# ---------------------------------------------------------------------------
# Category queries
# ---------------------------------------------------------------------------


class TestCategoryQueries:
    async def test_get_settings_by_category(self, db: AsyncSession):
        await SettingsCoreService.create_setting(
            db,
            key="cat_a.one",
            category="check",
            value=1,
            display_name="One",
        )
        await SettingsCoreService.create_setting(
            db,
            key="cat_a.two",
            category="check",
            value=2,
            display_name="Two",
        )
        await SettingsCoreService.create_setting(
            db,
            key="cat_b.one",
            category="alert",
            value=10,
            display_name="X",
        )

        rows = await SettingsCoreService.get_settings_by_category(db, "check")
        keys = {s.key for s in rows}
        assert keys == {"cat_a.one", "cat_a.two"}

    async def test_get_all_settings(self, db: AsyncSession):
        await SettingsCoreService.create_setting(
            db,
            key="all.a",
            category="system",
            value=1,
            display_name="A",
        )
        await SettingsCoreService.create_setting(
            db,
            key="all.b",
            category="system",
            value=2,
            display_name="B",
        )
        rows = await SettingsCoreService.get_all_settings(db)
        # Other tests in the session may have seeded settings — we only
        # require that ours show up.
        keys = {s.key for s in rows}
        assert "all.a" in keys
        assert "all.b" in keys


# ---------------------------------------------------------------------------
# Security settings facade
# ---------------------------------------------------------------------------


class TestSecuritySettings:
    async def test_ensure_then_get_returns_defaults(self, db: AsyncSession):
        """ensure_default_settings seeds the security.* keys if missing.
        get_security_settings then returns the dict facade."""
        await SettingsCoreService.ensure_default_settings(db)
        sec = await SettingsCoreService.get_security_settings(db)
        assert "max_failed_attempts" in sec
        assert "account_lock_duration_minutes" in sec
        assert "session_lifetime_days" in sec
        # Defaults should be sane integers
        assert sec["max_failed_attempts"] > 0
        assert sec["session_lifetime_days"] > 0

    async def test_get_without_ensure_returns_defaults_via_fallback(self, db: AsyncSession):
        """get_security_settings should be safe to call without prior
        ensure_default_settings — the per-key `get_setting` fallback
        provides default values."""
        sec = await SettingsCoreService.get_security_settings(db)
        assert isinstance(sec, dict)
        assert "max_failed_attempts" in sec

    async def test_update_security_settings(self, db: AsyncSession):
        await SettingsCoreService.ensure_default_settings(db)
        await SettingsCoreService.update_security_settings(
            db,
            max_failed_attempts=10,
            session_lifetime_days=30,
        )
        sec = await SettingsCoreService.get_security_settings(db)
        assert sec["max_failed_attempts"] == 10
        assert sec["session_lifetime_days"] == 30


# ---------------------------------------------------------------------------
# Other defaults helpers
# ---------------------------------------------------------------------------


class TestOtherDefaults:
    async def test_get_check_defaults_is_dict(self, db: AsyncSession):
        defaults = await SettingsCoreService.get_check_defaults(db)
        assert isinstance(defaults, dict)

    async def test_get_alert_defaults_is_dict(self, db: AsyncSession):
        defaults = await SettingsCoreService.get_alert_defaults(db)
        assert isinstance(defaults, dict)

    async def test_ensure_general_then_get(self, db: AsyncSession):
        await SettingsCoreService.ensure_default_settings(db)
        gen = await SettingsCoreService.get_general_settings(db)
        assert isinstance(gen, dict)

    async def test_ensure_metrics_then_get(self, db: AsyncSession):
        await SettingsCoreService.ensure_default_settings(db)
        m = await SettingsCoreService.get_metrics_settings(db)
        assert isinstance(m, dict)

    async def test_generate_metrics_token_returns_string(self, db: AsyncSession):
        await SettingsCoreService.ensure_default_settings(db)
        token = await SettingsCoreService.generate_metrics_token(db)
        assert isinstance(token, str)
        assert len(token) > 0
