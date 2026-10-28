"""Migration 014 removes the stored security.rate_limit_enabled row, and only it.

The suite's schema is already at head, so this runs the revision itself
against a row an older version seeded, beside the settings that stay.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.crud.setting_crud import SettingCRUD
from app.services.core.settings_core_service import SettingsCoreService
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

_REVISION = (
    Path(__file__).resolve().parent.parent.parent
    / "alembic"
    / "versions"
    / "014_drop_rate_limit_enabled_setting.py"
)


def _run(connection: Any, step: str) -> None:
    spec = importlib.util.spec_from_file_location("rev014", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, step)()


async def test_the_switch_row_goes_and_the_limits_stay(db: AsyncSession) -> None:
    await SettingsCoreService.ensure_default_settings(db)
    await SettingsCoreService.create_setting(
        db=db,
        key="security.rate_limit_enabled",
        category="security",
        value=True,
        display_name="Enable Rate Limiting",
        subcategory="Rate Limiting",
    )
    await db.flush()

    await db.run_sync(lambda s: _run(s.connection(), "upgrade"))

    assert await SettingCRUD.get_by_key(db, "security.rate_limit_enabled") is None
    assert await SettingCRUD.get_by_key(db, "security.login_rate_limit") is not None


async def test_the_seed_no_longer_creates_the_switch(db: AsyncSession) -> None:
    await SettingsCoreService.ensure_default_settings(db)

    assert await SettingCRUD.get_by_key(db, "security.rate_limit_enabled") is None
