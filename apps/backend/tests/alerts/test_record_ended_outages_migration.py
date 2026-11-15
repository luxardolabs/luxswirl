"""Migration 024 records the end of each outage stored without one.

The suite's schema is already at head, so this runs the revision's upgrade over
rows stored as before it, and reads what it added.
"""

from __future__ import annotations

import importlib.util
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.core.datetime_utils import utc_now
from fixtures.factories import (
    make_agent,
    make_alert,
    make_check,
    make_check_result,
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
    / "024_record_ended_outages.py"
)


def _run(connection: Any) -> None:
    spec = importlib.util.spec_from_file_location("rev024", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        module.upgrade()


async def _pair(
    db: AsyncSession,
    last_key: str,
    *,
    success: bool,
    latency: float | None,
    notify_on_recovery: bool = False,
) -> Any:
    """An alert+check whose last row has `last_key`, and the check's newest
    result."""
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(agent_id=agent.id)
    alert = make_alert(notify_on_recovery=notify_on_recovery)
    provider = make_notification_provider()
    db.add_all([check, alert, provider])
    await db.flush()
    then = utc_now() - timedelta(days=7)
    db.add(
        make_notification_log(
            alert_id=alert.id,
            notification_provider_id=provider.id,
            check_result_id=None,
            check_result_timestamp=then,
            check_id=check.id,
            notification_hash=last_key,
            sent_at=then,
        )
    )
    db.add(
        make_check_result(
            check_id=check.id, agent_id=agent.id, success=success, latency_ms=latency
        )
    )
    await db.flush()
    return check.id


async def _keys(db: AsyncSession, check_id: Any) -> list[str]:
    rows = await db.execute(
        text(
            "SELECT notification_hash FROM notification_logs WHERE check_id = :c "
            "ORDER BY sent_at, decision_seq"
        ),
        {"c": check_id},
    )
    return [row[0] for row in rows]


@pytest.mark.parametrize(
    ("last_key", "success", "latency", "added"),
    [
        ("status:down", True, 10.0, "status:up"),
        ("repeated_failure:down", True, 10.0, "repeated_failure:up"),
        ("threshold:latency_ms:>:100", True, 50.0, "threshold:latency_ms:>:100:ok"),
        ("status:down", False, 10.0, None),
        ("threshold:latency_ms:>:100", True, 500.0, None),
        ("status:up", True, 10.0, None),
    ],
    ids=[
        "status-ended",
        "repeated-ended",
        "threshold-ended",
        "status-still-down",
        "threshold-still-breached",
        "already-up",
    ],
)
async def test_an_ended_outage_is_recorded_once(
    db: AsyncSession,
    last_key: str,
    success: bool,
    latency: float | None,
    added: str | None,
) -> None:
    check_id = await _pair(db, last_key, success=success, latency=latency)

    await db.run_sync(lambda s: _run(s.connection()))

    assert await _keys(db, check_id) == [last_key] + ([added] if added else [])


async def test_an_alert_that_notifies_on_recovery_is_left_to_its_results(
    db: AsyncSession,
) -> None:
    """Its decisions announce the end; a row here would swallow that recovery."""
    check_id = await _pair(
        db, "status:down", success=True, latency=10.0, notify_on_recovery=True
    )

    await db.run_sync(lambda s: _run(s.connection()))

    assert await _keys(db, check_id) == ["status:down"]
