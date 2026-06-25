"""Integration tests for AlertCoreService CRUD + assignment paths.

Fills the coverage gap in alert_core_service.py — the basic CRUD methods
(create_alert, update_alert, delete_alert, list_alerts, get_alert_by_id),
notification provider attach/detach, check assignment.
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

from fixtures.factories import (  # noqa: E402
    make_agent,
    make_check,
    make_notification_provider,
)

from app.core.exceptions import NotFoundException  # noqa: E402
from app.crud.alert_crud import AlertCRUD  # noqa: E402
from app.schemas.alert_schema import AlertCreate, AlertUpdate  # noqa: E402
from app.services.core.alert_core_service import AlertCoreService  # noqa: E402

pytestmark = pytest.mark.integration


def _create_data(*, provider_ids=None, check_ids=None, **overrides) -> AlertCreate:
    defaults = {
        "name": f"test-alert-{uuid4().hex[:6]}",
        "trigger_type": "status_change",
        "trigger_config": {"on_status": ["error"], "consecutive_failures": 1},
        "is_enabled": True,
        "is_global": False,
        "notify_on_recovery": True,
        "notification_provider_ids": provider_ids or [],
        "check_ids": check_ids or [],
    }
    defaults.update(overrides)
    return AlertCreate(**defaults)


# ---------------------------------------------------------------------------
# create / get / update / delete
# ---------------------------------------------------------------------------


class TestCreateAlert:
    async def test_creates_with_no_mappings(self, db: AsyncSession):
        alert = await AlertCoreService.create_alert(db, _create_data())
        assert alert.id is not None
        assert alert.name.startswith("test-alert-")
        assert alert.is_enabled is True

    async def test_creates_with_provider_mappings(self, db: AsyncSession):
        p1 = make_notification_provider()
        p2 = make_notification_provider()
        db.add(p1)
        db.add(p2)
        await db.flush()

        alert = await AlertCoreService.create_alert(
            db,
            _create_data(provider_ids=[p1.id, p2.id]),
        )
        # Query CRUD directly — relying on relationship refresh is brittle
        # because the mappings weren't pre-loaded on the alert instance.
        mappings = await AlertCRUD.list_active_notif_mappings_for_alert(
            db,
            alert.id,
        )
        assert len(mappings) == 2

    async def test_global_alert_creates_with_no_check_ids(self, db: AsyncSession):
        """Global alerts apply to all checks dynamically — created without
        explicit check_ids (the schema's check_ids is list[int], which seems
        legacy, and global alerts use a separate path anyway)."""
        alert = await AlertCoreService.create_alert(
            db,
            _create_data(is_global=True, check_ids=[]),
        )
        assert alert.is_global is True


class TestGetAlert:
    async def test_get_by_id(self, db: AsyncSession):
        alert = await AlertCoreService.create_alert(db, _create_data())
        loaded = await AlertCoreService.get_alert_by_id(db, alert.id)
        assert loaded.id == alert.id

    async def test_missing_raises(self, db: AsyncSession):
        with pytest.raises(NotFoundException):
            await AlertCoreService.get_alert_by_id(db, uuid4())


class TestListAlerts:
    async def test_pagination_and_total(self, db: AsyncSession):
        for i in range(5):
            await AlertCoreService.create_alert(
                db,
                _create_data(name=f"alert-{i:02d}"),
            )
        rows, total = await AlertCoreService.list_alerts(db, skip=1, limit=2)
        assert total == 5
        assert len(rows) == 2

    async def test_filter_by_is_enabled(self, db: AsyncSession):
        await AlertCoreService.create_alert(db, _create_data(is_enabled=True))
        await AlertCoreService.create_alert(db, _create_data(is_enabled=False))
        rows, total = await AlertCoreService.list_alerts(db, is_enabled=True)
        assert all(a.is_enabled for a in rows)

    async def test_filter_by_is_global(self, db: AsyncSession):
        await AlertCoreService.create_alert(db, _create_data(is_global=True))
        await AlertCoreService.create_alert(db, _create_data(is_global=False))
        rows, _ = await AlertCoreService.list_alerts(db, is_global=True)
        assert all(a.is_global for a in rows)


class TestUpdateAlert:
    async def test_updates_name_and_resend_interval(self, db: AsyncSession):
        alert = await AlertCoreService.create_alert(db, _create_data())
        updated = await AlertCoreService.update_alert(
            db,
            alert.id,
            AlertUpdate(name="renamed", resend_interval_minutes=30),
        )
        assert updated.name == "renamed"
        assert updated.resend_interval_minutes == 30

    async def test_partial_preserves_others(self, db: AsyncSession):
        alert = await AlertCoreService.create_alert(
            db,
            _create_data(name="original", is_enabled=True),
        )
        updated = await AlertCoreService.update_alert(
            db,
            alert.id,
            AlertUpdate(is_enabled=False),
        )
        assert updated.is_enabled is False
        assert updated.name == "original"

    async def test_missing_alert_raises(self, db: AsyncSession):
        with pytest.raises(NotFoundException):
            await AlertCoreService.update_alert(
                db,
                uuid4(),
                AlertUpdate(name="x"),
            )


class TestDeleteAlert:
    async def test_soft_delete_excludes_from_list(self, db: AsyncSession):
        alert = await AlertCoreService.create_alert(db, _create_data())
        await AlertCoreService.delete_alert(db, alert.id)
        with pytest.raises(NotFoundException):
            await AlertCoreService.get_alert_by_id(db, alert.id)

    async def test_missing_alert_raises(self, db: AsyncSession):
        with pytest.raises(NotFoundException):
            await AlertCoreService.delete_alert(db, uuid4())


# ---------------------------------------------------------------------------
# Provider mapping management
# ---------------------------------------------------------------------------


class TestProviderMappings:
    async def test_add_notification_provider(self, db: AsyncSession):
        alert = await AlertCoreService.create_alert(db, _create_data())
        provider = make_notification_provider()
        db.add(provider)
        await db.flush()

        await AlertCoreService.add_notification_provider(db, alert.id, provider.id)
        # Verify mapping
        from app.crud.alert_crud import AlertCRUD

        mapping = await AlertCRUD.get_notif_mapping(db, alert.id, provider.id)
        assert mapping is not None

    async def test_remove_notification_provider(self, db: AsyncSession):
        provider = make_notification_provider()
        db.add(provider)
        await db.flush()
        alert = await AlertCoreService.create_alert(
            db,
            _create_data(provider_ids=[provider.id]),
        )

        await AlertCoreService.remove_notification_provider(
            db,
            alert.id,
            provider.id,
        )
        from app.crud.alert_crud import AlertCRUD

        mapping = await AlertCRUD.get_notif_mapping(db, alert.id, provider.id)
        assert mapping is None


# ---------------------------------------------------------------------------
# Check assignment
# ---------------------------------------------------------------------------


class TestCheckAssignment:
    async def test_add_check_creates_mapping(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        check = make_check(agent_id=agent.id)
        db.add(check)
        await db.flush()
        alert = await AlertCoreService.create_alert(db, _create_data())

        await AlertCoreService.add_check(db, alert.id, check.id)
        ids = await AlertCoreService.get_alert_ids_for_check(db, check.id)
        assert alert.id in ids

    async def test_remove_check_drops_mapping(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        check = make_check(agent_id=agent.id)
        db.add(check)
        await db.flush()
        alert = await AlertCoreService.create_alert(db, _create_data())
        await AlertCoreService.add_check(db, alert.id, check.id)

        await AlertCoreService.remove_check(db, alert.id, check.id)
        ids = await AlertCoreService.get_alert_ids_for_check(db, check.id)
        assert alert.id not in ids

    async def test_bulk_assign_to_checks(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        checks = [make_check(agent_id=agent.id) for _ in range(3)]
        for c in checks:
            db.add(c)
        await db.flush()
        alert = await AlertCoreService.create_alert(db, _create_data())

        await AlertCoreService.bulk_assign_to_checks(
            db,
            alert.id,
            [c.id for c in checks],
        )
        for c in checks:
            assert alert.id in await AlertCoreService.get_alert_ids_for_check(db, c.id)

    async def test_bulk_clear_from_checks(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        checks = [make_check(agent_id=agent.id) for _ in range(3)]
        for c in checks:
            db.add(c)
        await db.flush()
        alert = await AlertCoreService.create_alert(db, _create_data())
        await AlertCoreService.bulk_assign_to_checks(
            db,
            alert.id,
            [c.id for c in checks],
        )

        # bulk_clear_from_checks clears ALL alerts from the given checks
        # — it doesn't target a single alert.
        await AlertCoreService.bulk_clear_from_checks(db, [c.id for c in checks])
        for c in checks:
            assert alert.id not in await AlertCoreService.get_alert_ids_for_check(
                db,
                c.id,
            )


# ---------------------------------------------------------------------------
# Snooze
# ---------------------------------------------------------------------------


class TestSnooze:
    async def test_snooze_then_unsnooze(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        check = make_check(agent_id=agent.id)
        db.add(check)
        await db.flush()
        alert = await AlertCoreService.create_alert(db, _create_data())
        await AlertCoreService.add_check(db, alert.id, check.id)

        # Snooze 60 minutes
        mapping = await AlertCoreService.snooze_alert_check(
            db,
            alert.id,
            check.id,
            minutes=60,
        )
        assert mapping.snoozed_until is not None

        # Unsnooze
        await AlertCoreService.unsnooze_alert_check(db, alert.id, check.id)
        from app.crud.alert_crud import AlertCRUD

        reloaded = await AlertCRUD.get_check_mapping(db, alert.id, check.id)
        assert reloaded.snoozed_until is None
