"""Migration 023 renames a repeated_failure alert's stored state key.

The suite's schema is already at head, so this runs the revision's upgrade over
rows stored as before it, and reads what it left.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fixtures.factories import (
    make_alert,
    make_notification_log,
    make_notification_provider,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

_REVISION = (
    Path(__file__).resolve().parent.parent.parent
    / "alembic"
    / "versions"
    / "023_repeated_failure_state_keys.py"
)


def _run(connection: Any, step: str) -> None:
    spec = importlib.util.spec_from_file_location("rev023", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, step)()


async def test_the_upgrade_reads_a_stored_active_key_as_the_outage(
    db: AsyncSession,
) -> None:
    alert = make_alert(trigger_type="repeated_failure")
    provider = make_notification_provider()
    db.add_all([alert, provider])
    await db.flush()
    keys = ["repeated_failure:active", "status:down", "ssl:lte:7"]
    rows = [
        make_notification_log(
            alert_id=alert.id,
            notification_provider_id=provider.id,
            check_result_id=None,
            check_result_timestamp=None,
            notification_hash=key,
        )
        for key in keys
    ]
    db.add_all(rows)
    await db.flush()

    await db.run_sync(lambda s: _run(s.connection(), "upgrade"))

    stored = (
        await db.execute(
            text(
                "SELECT id, notification_hash FROM notification_logs WHERE id = ANY(:i)"
            ),
            {"i": [row.id for row in rows]},
        )
    ).all()
    assert dict(stored) == {
        rows[0].id: "repeated_failure:down",
        rows[1].id: "status:down",
        rows[2].id: "ssl:lte:7",
    }
