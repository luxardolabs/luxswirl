"""Migration 017 resets each stored rate limit that is not a limit to the environment's.

A `security.*_rate_limit` setting saved before its value was validated can hold
text slowapi cannot parse (`10/15mins`), a bare non-string, or a limit of zero
requests. The server already leaves the environment's limit in effect for such
a row and shows it as not applied; the revision rewrites the row to the
environment's limit, so the stored value is one again. A valid limit an
operator set is kept, and a second run changes nothing.

The suite's schema is already at head, so this runs the revision itself against
rows shaped like an older version's.
"""

from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.core.config import settings
from app.services.core.settings_core_service import SettingsCoreService
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

_REVISION = (
    Path(__file__).resolve().parent.parent.parent
    / "alembic"
    / "versions"
    / "017_reset_invalid_rate_limits.py"
)


def _revision() -> ModuleType:
    spec = importlib.util.spec_from_file_location("rev017", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade(connection: Any) -> None:
    with Operations.context(MigrationContext.configure(connection)):
        _revision().upgrade()


async def _store(db: AsyncSession, key: str, stored: object) -> None:
    """Put exactly ``stored`` (the JSON the column holds) in ``key``'s value."""
    await db.execute(
        text("UPDATE settings SET value = CAST(:v AS json) WHERE key = :k"),
        {"v": json.dumps(stored), "k": key},
    )


async def _value(db: AsyncSession, key: str) -> object:
    raw = await db.scalar(
        text("SELECT CAST(value AS text) FROM settings WHERE key = :k"), {"k": key}
    )
    return json.loads(raw)


async def _run(db: AsyncSession) -> None:
    await db.run_sync(lambda s: _upgrade(s.connection()))


async def test_a_stored_value_that_is_not_a_limit_takes_the_environments(
    db: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    await SettingsCoreService.ensure_default_settings(db)
    await _store(
        db, "security.login_rate_limit", {"value": "10/15mins", "type": "string"}
    )
    await db.flush()

    with caplog.at_level(logging.WARNING, logger="alembic.runtime.migration"):
        await _run(db)

    assert await _value(db, "security.login_rate_limit") == {
        "value": settings.security.login_rate_limit,
        "type": "string",
    }
    assert "security.login_rate_limit" in caplog.text


@pytest.mark.parametrize(
    "stored",
    [
        5,  # a bare value, written outside the app
        {"value": 100, "type": "int"},  # not text at all
        {"value": "0/minute", "type": "string"},  # no request allowed
        {"value": "1/1000years", "type": "string"},  # a period past a year
        {"value": "1/minute;" * 20, "type": "string"},  # longer than any limit
        {"value": None, "type": "string"},
        {"value": "", "type": "string"},
        {"type": "string"},  # no value key
    ],
)
async def test_every_shape_that_is_no_limit_is_reset(
    db: AsyncSession, stored: object
) -> None:
    await SettingsCoreService.ensure_default_settings(db)
    await _store(db, "security.api_rate_limit", stored)
    await db.flush()

    await _run(db)

    assert await _value(db, "security.api_rate_limit") == {
        "value": settings.security.api_rate_limit,
        "type": "string",
    }


async def test_a_valid_limit_an_operator_set_is_kept_and_a_rerun_changes_nothing(
    db: AsyncSession,
) -> None:
    await SettingsCoreService.ensure_default_settings(db)
    kept = {"value": "7/hour", "type": "string"}
    await _store(db, "security.registration_rate_limit", kept)
    await _store(db, "security.login_rate_limit", {"value": "x", "type": "string"})
    await db.flush()

    await _run(db)
    first = await _value(db, "security.login_rate_limit")
    await _run(db)

    assert await _value(db, "security.registration_rate_limit") == kept
    assert await _value(db, "security.login_rate_limit") == first


async def test_only_the_rate_limit_settings_are_touched(db: AsyncSession) -> None:
    await SettingsCoreService.ensure_default_settings(db)
    # A string setting that is not a rate limit, holding text that is not one.
    await _store(db, "general.timezone", {"value": "10/15mins", "type": "string"})
    await db.flush()

    await _run(db)

    assert await _value(db, "general.timezone") == {
        "value": "10/15mins",
        "type": "string",
    }


async def test_after_the_reset_startup_applies_it_as_the_environments(
    db: AsyncSession,
) -> None:
    """The reset row reads as following the environment: startup applies it
    and the Settings page no longer shows it as not applied."""
    await SettingsCoreService.ensure_default_settings(db)
    await _store(
        db, "security.login_rate_limit", {"value": "10/15mins", "type": "string"}
    )
    await db.flush()

    await _run(db)
    await SettingsCoreService.load_rate_limits(db)

    setting = await SettingsCoreService.get_setting_response(
        db, "security.login_rate_limit"
    )
    assert setting.applied is True
