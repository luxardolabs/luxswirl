"""A failure to wire a check to an alert must reach the caller.

Both surfaces used to log a warning and carry on, so the form returned success
while the check was attached to a different set of alerts than the operator had
just chosen. On a monitoring product that is the worst shape a bug can take: the
check runs, an outage happens, nothing is notified, and nothing anywhere says
the wiring never took. These tests fail if either failure is swallowed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.exceptions import ValidationException
from app.services.core.alert_core_service import AlertCoreService
from app.services.core.check_core_service import CheckCoreService
from fixtures.factories import make_agent, make_alert, make_check
from sqlalchemy.ext.asyncio import AsyncSession


async def _check_and_alert(db: AsyncSession):
    agent = make_agent(agent_name="wiring-01")
    db.add(agent)
    await db.flush()
    check = make_check(agent_id=agent.id, display_name="wiring-check")
    alert = make_alert(name="wiring-alert")
    db.add_all([check, alert])
    await db.flush()
    return check, alert


class TestAlertCoreServiceSyncCheckAlerts:
    async def test_a_failure_propagates_instead_of_being_logged(
        self, db: AsyncSession, monkeypatch
    ):
        check, alert = await _check_and_alert(db)

        async def _boom(*_args, **_kwargs):
            raise RuntimeError("mapping insert failed")

        monkeypatch.setattr(AlertCoreService, "add_check", _boom)

        with pytest.raises(RuntimeError):
            await AlertCoreService.sync_check_alerts(db, check.id, [alert.id])

    async def test_a_removal_failure_propagates_too(
        self, db: AsyncSession, monkeypatch
    ):
        check, alert = await _check_and_alert(db)
        await AlertCoreService.add_check(db, alert.id, check.id)
        await db.flush()

        async def _boom(*_args, **_kwargs):
            raise RuntimeError("mapping delete failed")

        monkeypatch.setattr(AlertCoreService, "remove_check", _boom)

        # Assigning an EMPTY list is how the form says "detach from everything".
        with pytest.raises(RuntimeError):
            await AlertCoreService.sync_check_alerts(db, check.id, [])

    async def test_the_happy_path_still_wires_both_directions(self, db: AsyncSession):
        check, alert = await _check_and_alert(db)

        await AlertCoreService.sync_check_alerts(db, check.id, [alert.id])
        assert await AlertCoreService.get_alert_ids_for_check(db, check.id) == {
            alert.id
        }

        await AlertCoreService.sync_check_alerts(db, check.id, [])
        assert await AlertCoreService.get_alert_ids_for_check(db, check.id) == set()


class TestCheckCoreServiceAssignAlertsToCheck:
    async def test_already_assigned_is_satisfied_not_an_error(self, db: AsyncSession):
        """This entry point takes a raw list, not a set difference, so a repeat
        assignment is the state the caller asked for — and only THAT is caught."""
        check, alert = await _check_and_alert(db)

        await CheckCoreService.assign_alerts(db, check.id, [alert.id])
        await CheckCoreService.assign_alerts(db, check.id, [alert.id])

        assert await AlertCoreService.get_alert_ids_for_check(db, check.id) == {
            alert.id
        }

    async def test_anything_else_propagates(self, db: AsyncSession, monkeypatch):
        check, _alert = await _check_and_alert(db)

        async def _boom(*_args, **_kwargs):
            raise RuntimeError("mapping insert failed")

        monkeypatch.setattr(AlertCoreService, "add_check", _boom)

        with pytest.raises(RuntimeError):
            await CheckCoreService.assign_alerts(db, check.id, [uuid4()])

    async def test_a_validation_error_is_the_only_one_absorbed(
        self, db: AsyncSession, monkeypatch
    ):
        """Narrow by TYPE, not by breadth: a ValidationException that is NOT the
        duplicate case is still absorbed here, which is the price of catching by
        type — but everything outside that type must still surface."""
        check, alert = await _check_and_alert(db)

        async def _validation(*_args, **_kwargs):
            raise ValidationException("Check already added to this alert")

        monkeypatch.setattr(AlertCoreService, "add_check", _validation)
        await CheckCoreService.assign_alerts(db, check.id, [alert.id])  # no raise
