"""Migration 013 sets alerts.trigger_type's comment to the model's, and back.

The suite's schema is already at head, so this runs the revision itself, down
and up again, and reads the comment the database holds after each step.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.models.alert_model import Alert
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

_REVISION = (
    Path(__file__).resolve().parent.parent.parent
    / "alembic"
    / "versions"
    / "013_alert_trigger_type_comment.py"
)


def _run(connection: Any, step: str) -> None:
    spec = importlib.util.spec_from_file_location("rev013", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, step)()


async def _comment(db: AsyncSession) -> str | None:
    return (
        await db.execute(
            text(
                "SELECT col_description('alerts'::regclass, attnum) FROM pg_attribute"
                " WHERE attrelid = 'alerts'::regclass AND attname = 'trigger_type'"
            )
        )
    ).scalar_one()


async def test_upgrade_names_every_trigger_type_and_downgrade_restores(
    db: AsyncSession,
) -> None:
    await db.run_sync(lambda s: _run(s.connection(), "downgrade"))
    downgraded = await _comment(db)
    await db.run_sync(lambda s: _run(s.connection(), "upgrade"))
    upgraded = await _comment(db)

    assert downgraded is not None
    assert "no_data" not in downgraded
    assert upgraded == Alert.__table__.c.trigger_type.comment
