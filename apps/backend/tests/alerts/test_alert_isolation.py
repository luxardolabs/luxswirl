"""One alert that fails while it is evaluated stops no other alert on the check.

A stored trigger_config of the wrong type, or any other per-alert bug, used to
leave the evaluation loop: the alerts after it on that check never fired.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest
from app.services.core.alert_evaluation_core_service import AlertEvaluationCoreService
from fixtures.factories import (
    make_agent,
    make_alert,
    make_alert_check_mapping,
    make_check,
    make_check_result,
)
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def test_an_alert_that_raises_leaves_the_others_to_fire(
    db: AsyncSession, app_logs: list[logging.LogRecord]
) -> None:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(agent_id=agent.id)
    broken = make_alert(name="broken")
    good = make_alert(name="good")
    db.add_all([check, broken, good])
    await db.flush()
    db.add_all(
        [
            make_alert_check_mapping(alert_id=broken.id, check_id=check.id),
            make_alert_check_mapping(alert_id=good.id, check_id=check.id),
        ]
    )
    result = make_check_result(check_id=check.id, agent_id=agent.id, success=False)
    db.add(result)
    await db.flush()
    fired: list[UUID] = []

    async def should_fire(_db: Any, alert: Any, _check: Any, _result: Any) -> bool:
        if alert.id == broken.id:
            raise TypeError("'>' not supported between instances of 'str' and 'int'")
        return True

    async def fire(_db: Any, alert: Any, _check: Any, _result: Any) -> None:
        fired.append(alert.id)

    with (
        patch.object(AlertEvaluationCoreService, "_should_fire_alert", should_fire),
        patch.object(AlertEvaluationCoreService, "_fire_alert", fire),
    ):
        await AlertEvaluationCoreService.evaluate_check_result(db, result)

    assert fired == [good.id]
    failures = [
        r
        for r in app_logs
        if r.levelno >= logging.ERROR and str(broken.id) in str(r.__dict__)
    ]
    assert failures, [r.getMessage() for r in app_logs]
