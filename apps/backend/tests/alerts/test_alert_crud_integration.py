"""Integration tests for AlertCRUD against a real TimescaleDB.

These tests exercise the dedup-related queries that the alert subsystem
depends on. They are the canonical example for how to write integration
tests in this project — every other domain should mirror this pattern.

Run with:
    docker compose -f compose.test.yaml up -d --wait
    docker compose -f compose.test.yaml run --rm tests \\
        pytest tests/alerts/test_alert_crud_integration.py -v

The `db` fixture (imported below) gives each test an isolated AsyncSession
in a transaction that's rolled back at the end — no test pollutes the next.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

# Allow `from fixtures.X import Y` resolution. conftest does this for the
# pytest entry point; standalone tools (e.g. mutmut, ad-hoc imports) need it
# too. Cheap idempotent insert.
_tests_root = str(Path(__file__).resolve().parent.parent)
if _tests_root not in sys.path:
    sys.path.insert(0, _tests_root)

from fixtures.factories import (  # noqa: E402
    make_agent,
    make_alert,
    make_alert_check_mapping,
    make_alert_notification_mapping,
    make_check,
    make_check_result,
    make_notification_log,
    make_notification_provider,
)

from app.core.datetime_utils import utc_now  # noqa: E402
from app.crud.alert_crud import DEDUP_RELEVANT_STATUSES, AlertCRUD  # noqa: E402

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------


async def _make_alert_with_check_and_provider(db: AsyncSession):
    """Common scaffolding: an enabled alert mapped to one check and one
    enabled provider. Returns (alert, check, provider, agent)."""
    agent = make_agent()
    db.add(agent)
    await db.flush()

    check = make_check(agent_id=agent.id)
    db.add(check)
    await db.flush()

    alert = make_alert()
    provider = make_notification_provider()
    db.add(alert)
    db.add(provider)
    await db.flush()

    db.add(make_alert_check_mapping(alert_id=alert.id, check_id=check.id))
    db.add(make_alert_notification_mapping(alert_id=alert.id, notification_provider_id=provider.id))
    await db.flush()

    return alert, check, provider, agent


async def _write_log(
    db: AsyncSession,
    *,
    alert,
    check,
    provider,
    agent,
    status: str,
    notification_hash: str | None,
    sent_at=None,
):
    """Insert one CheckResult + NotificationLog pair and return the log."""
    result = make_check_result(check_id=check.id, agent_id=agent.id)
    db.add(result)
    await db.flush()

    log = make_notification_log(
        alert_id=alert.id,
        notification_provider_id=provider.id,
        check_result_id=result.id,
        check_result_timestamp=result.timestamp,
        check_id=check.id,
        status=status,
        notification_hash=notification_hash,
        sent_at=sent_at or utc_now(),
    )
    db.add(log)
    await db.flush()
    return log


# ---------------------------------------------------------------------------
# get_last_notification_for_dedup — the LUXSWIRL-145 fix lives here
# ---------------------------------------------------------------------------


class TestGetLastNotificationForDedup:
    async def test_no_history_returns_none(self, db: AsyncSession):
        alert, check, _, _ = await _make_alert_with_check_and_provider(db)
        result = await AlertCRUD.get_last_notification_for_dedup(db, alert.id, check.id)
        assert result is None

    async def test_returns_most_recent_sent_row(self, db: AsyncSession):
        alert, check, provider, agent = await _make_alert_with_check_and_provider(db)
        older = await _write_log(
            db,
            alert=alert,
            check=check,
            provider=provider,
            agent=agent,
            status="sent",
            notification_hash="status:down",
            sent_at=utc_now() - timedelta(hours=1),
        )
        newer = await _write_log(
            db,
            alert=alert,
            check=check,
            provider=provider,
            agent=agent,
            status="sent",
            notification_hash="status:up",
            sent_at=utc_now(),
        )
        result = await AlertCRUD.get_last_notification_for_dedup(db, alert.id, check.id)
        assert result is not None
        assert result.id == newer.id
        assert result.id != older.id

    async def test_returns_suppressed_row_when_only_history(self, db: AsyncSession):
        """The whole point of LUXSWIRL-145: dedup must see suppressed/failed
        rows, not just sent rows. Otherwise dedup amnesia floods the logs."""
        alert, check, provider, agent = await _make_alert_with_check_and_provider(db)
        await _write_log(
            db,
            alert=alert,
            check=check,
            provider=provider,
            agent=agent,
            status="suppressed",
            notification_hash="status:down",
        )
        result = await AlertCRUD.get_last_notification_for_dedup(db, alert.id, check.id)
        assert result is not None
        assert result.status == "suppressed"
        assert result.notification_hash == "status:down"

    async def test_returns_failed_row(self, db: AsyncSession):
        alert, check, provider, agent = await _make_alert_with_check_and_provider(db)
        await _write_log(
            db,
            alert=alert,
            check=check,
            provider=provider,
            agent=agent,
            status="failed",
            notification_hash="status:down",
        )
        result = await AlertCRUD.get_last_notification_for_dedup(db, alert.id, check.id)
        assert result is not None
        assert result.status == "failed"

    async def test_returns_rate_limited_row(self, db: AsyncSession):
        alert, check, provider, agent = await _make_alert_with_check_and_provider(db)
        await _write_log(
            db,
            alert=alert,
            check=check,
            provider=provider,
            agent=agent,
            status="rate_limited",
            notification_hash="status:down",
        )
        result = await AlertCRUD.get_last_notification_for_dedup(db, alert.id, check.id)
        assert result is not None
        assert result.status == "rate_limited"

    async def test_other_alert_does_not_contaminate(self, db: AsyncSession):
        """Two alerts on the same check — dedup lookup is per (alert, check)."""
        alert_a, check, provider, agent = await _make_alert_with_check_and_provider(db)
        alert_b = make_alert(name="other-alert")
        db.add(alert_b)
        await db.flush()
        db.add(make_alert_check_mapping(alert_id=alert_b.id, check_id=check.id))
        db.add(
            make_alert_notification_mapping(
                alert_id=alert_b.id, notification_provider_id=provider.id
            )
        )
        await db.flush()

        log_a = await _write_log(
            db,
            alert=alert_a,
            check=check,
            provider=provider,
            agent=agent,
            status="sent",
            notification_hash="status:down",
        )
        log_b = await _write_log(
            db,
            alert=alert_b,
            check=check,
            provider=provider,
            agent=agent,
            status="sent",
            notification_hash="ssl:lte:7",
        )

        # Lookup for alert_a must return log_a, not log_b
        result_a = await AlertCRUD.get_last_notification_for_dedup(db, alert_a.id, check.id)
        assert result_a is not None
        assert result_a.id == log_a.id
        assert result_a.notification_hash == "status:down"

        result_b = await AlertCRUD.get_last_notification_for_dedup(db, alert_b.id, check.id)
        assert result_b is not None
        assert result_b.id == log_b.id
        assert result_b.notification_hash == "ssl:lte:7"

    async def test_other_check_does_not_contaminate(self, db: AsyncSession):
        """Two checks under the same alert — dedup lookup is per (alert, check)."""
        alert, check_a, provider, agent = await _make_alert_with_check_and_provider(db)
        check_b = make_check(agent_id=agent.id)
        db.add(check_b)
        await db.flush()
        db.add(make_alert_check_mapping(alert_id=alert.id, check_id=check_b.id))
        await db.flush()

        log_a = await _write_log(
            db,
            alert=alert,
            check=check_a,
            provider=provider,
            agent=agent,
            status="sent",
            notification_hash="status:down",
        )
        log_b = await _write_log(
            db,
            alert=alert,
            check=check_b,
            provider=provider,
            agent=agent,
            status="suppressed",
            notification_hash="parent_down",
        )

        result_a = await AlertCRUD.get_last_notification_for_dedup(db, alert.id, check_a.id)
        assert result_a.id == log_a.id

        result_b = await AlertCRUD.get_last_notification_for_dedup(db, alert.id, check_b.id)
        assert result_b.id == log_b.id


# ---------------------------------------------------------------------------
# Sanity check: the constant the CRUD relies on
# ---------------------------------------------------------------------------


class TestDedupRelevantStatuses:
    def test_includes_all_audit_statuses(self):
        """If a new status enum member is added that represents an audit row,
        it must be added to DEDUP_RELEVANT_STATUSES or dedup amnesia comes back."""
        assert "sent" in DEDUP_RELEVANT_STATUSES
        assert "failed" in DEDUP_RELEVANT_STATUSES
        assert "rate_limited" in DEDUP_RELEVANT_STATUSES
        assert "suppressed" in DEDUP_RELEVANT_STATUSES
        assert "deduplicated" in DEDUP_RELEVANT_STATUSES

    def test_excludes_retrying(self):
        """`retrying` represents a pending decision, not a recorded state.
        Including it would let in-flight retries spoof dedup precedent."""
        assert "retrying" not in DEDUP_RELEVANT_STATUSES


# ---------------------------------------------------------------------------
# list_active_alerts_for_check — gates which alerts apply
# ---------------------------------------------------------------------------


class TestListActiveAlertsForCheck:
    async def test_returns_enabled_mapped_alert(self, db: AsyncSession):
        alert, check, _, _ = await _make_alert_with_check_and_provider(db)
        results = await AlertCRUD.list_active_alerts_for_check(db, check.id)
        assert len(results) == 1
        assert results[0].id == alert.id

    async def test_excludes_disabled_alert(self, db: AsyncSession):
        alert, check, _, _ = await _make_alert_with_check_and_provider(db)
        alert.is_enabled = False
        await db.flush()
        results = await AlertCRUD.list_active_alerts_for_check(db, check.id)
        assert results == []

    async def test_excludes_soft_deleted_alert(self, db: AsyncSession):
        alert, check, _, _ = await _make_alert_with_check_and_provider(db)
        alert.deleted_at = utc_now()
        await db.flush()
        results = await AlertCRUD.list_active_alerts_for_check(db, check.id)
        assert results == []

    async def test_excludes_disabled_mapping(self, db: AsyncSession):
        """Mapping enable flag is independent of the alert's own enable flag —
        an alert can be enabled globally but disabled for a specific check."""
        alert, check, _, _ = await _make_alert_with_check_and_provider(db)
        from sqlalchemy import select

        from app.models.alert_check_mapping_model import AlertCheckMapping

        result = await db.execute(
            select(AlertCheckMapping).where(
                AlertCheckMapping.alert_id == alert.id,
                AlertCheckMapping.check_id == check.id,
            )
        )
        mapping = result.scalar_one()
        mapping.is_enabled = False
        await db.flush()

        results = await AlertCRUD.list_active_alerts_for_check(db, check.id)
        assert results == []

    async def test_unmapped_check_returns_empty(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        orphan = make_check(agent_id=agent.id)
        db.add(orphan)
        await db.flush()

        results = await AlertCRUD.list_active_alerts_for_check(db, orphan.id)
        assert results == []
