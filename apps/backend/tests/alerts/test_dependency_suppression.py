"""Unit tests for parent-check dependency suppression.

Covers:
- AlertEvaluationCoreService._handle_parent_suppression (cascade gate)
- CheckCoreService._validate_dependency (single-level + self-ref rules)

CRUD calls are mocked; no database fixture required.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from app.core.exceptions import ValidationException
from app.services.core.alert_evaluation_core_service import AlertEvaluationCoreService
from app.services.core.check_core_service import CheckCoreService

pytestmark = pytest.mark.unit  # all tests in this module are pure-logic


def _alert():
    return SimpleNamespace(id=uuid4(), name="alert-x")


def _check(parent_id=None, name="child", **kw):
    return SimpleNamespace(
        id=kw.get("id", uuid4()),
        display_name=name,
        depends_on_check_id=parent_id,
        parent_check=kw.get("parent_check"),
    )


def _check_result():
    return SimpleNamespace(
        id=uuid4(),
        timestamp="2026-05-11T00:00:00Z",
        success=False,
        latency_ms=42.0,
    )


def _provider_mapping(provider_id=None, deleted=False):
    return SimpleNamespace(
        notification_provider=SimpleNamespace(
            id=provider_id or uuid4(),
            deleted_at=("x" if deleted else None),
        )
    )


@pytest.fixture
def written():
    """The suppression rows handed to the crud write (the row is the behaviour)."""
    with patch(
        "app.services.core.alert_evaluation_core_service.AlertCRUD.add_notification_logs",
        AsyncMock(),
    ) as add_logs:
        yield add_logs


def _logs(written: AsyncMock) -> list:
    written.assert_awaited_once()
    return list(written.await_args.args[1])


@pytest.mark.asyncio
async def test_suppression_no_parent_returns_false(written):
    db = AsyncMock()
    check = _check(parent_id=None)
    result = await AlertEvaluationCoreService._handle_parent_suppression(
        db, _alert(), check, _check_result()
    )
    assert result is False
    written.assert_not_awaited()


@pytest.mark.asyncio
async def test_suppression_parent_no_results_fails_open(written):
    db = AsyncMock()
    parent_id = uuid4()
    check = _check(parent_id=parent_id)
    with patch(
        "app.services.core.alert_evaluation_core_service.CheckResultCRUD.get_latest_result_for_check",
        AsyncMock(return_value=None),
    ):
        result = await AlertEvaluationCoreService._handle_parent_suppression(
            db, _alert(), check, _check_result()
        )
    assert result is False
    written.assert_not_awaited()


@pytest.mark.asyncio
async def test_suppression_parent_up_returns_false(written):
    db = AsyncMock()
    parent_id = uuid4()
    check = _check(parent_id=parent_id)
    with patch(
        "app.services.core.alert_evaluation_core_service.CheckResultCRUD.get_latest_result_for_check",
        AsyncMock(return_value=SimpleNamespace(success=True)),
    ):
        result = await AlertEvaluationCoreService._handle_parent_suppression(
            db, _alert(), check, _check_result()
        )
    assert result is False
    written.assert_not_awaited()


@pytest.mark.asyncio
async def test_suppression_parent_down_first_occurrence_writes_log_per_provider(
    written,
):
    """First time leaf enters parent-down: write a suppressed row per active provider."""
    db = AsyncMock()
    parent_id = uuid4()
    parent = SimpleNamespace(display_name="gateway")
    check = _check(parent_id=parent_id, parent_check=parent)
    mappings = [
        _provider_mapping(),
        _provider_mapping(),
        _provider_mapping(deleted=True),
    ]

    with (
        patch(
            "app.services.core.alert_evaluation_core_service.CheckResultCRUD.get_latest_result_for_check",
            AsyncMock(return_value=SimpleNamespace(success=False)),
        ),
        patch(
            "app.services.core.alert_evaluation_core_service.AlertCRUD.list_active_notif_mappings_for_alert",
            AsyncMock(return_value=mappings),
        ),
        patch(
            "app.services.core.alert_evaluation_core_service.AlertCRUD.get_last_notification_for_dedup",
            AsyncMock(return_value=None),  # no prior notification — first occurrence
        ),
    ):
        result = await AlertEvaluationCoreService._handle_parent_suppression(
            db, _alert(), check, _check_result()
        )

    assert result is True
    logs = _logs(written)
    assert len(logs) == 2  # third mapping was soft-deleted
    logged = logs[0]
    assert logged.status == "suppressed"
    assert "gateway" in logged.error_message
    assert logged.notification_hash == "parent_down"


@pytest.mark.asyncio
async def test_suppression_parent_down_steady_state_dedups(monkeypatch, written):
    """LUXSWIRL-148: continuously-parent-down leaf doesn't write a row every tick."""
    db = AsyncMock()
    parent_id = uuid4()
    check = _check(
        parent_id=parent_id, parent_check=SimpleNamespace(display_name="gateway")
    )
    last_row = SimpleNamespace(notification_hash="parent_down")

    with (
        patch(
            "app.services.core.alert_evaluation_core_service.CheckResultCRUD.get_latest_result_for_check",
            AsyncMock(return_value=SimpleNamespace(success=False)),
        ),
        patch(
            "app.services.core.alert_evaluation_core_service.AlertCRUD.get_last_notification_for_dedup",
            AsyncMock(return_value=last_row),
        ),
    ):
        result = await AlertEvaluationCoreService._handle_parent_suppression(
            db, _alert(), check, _check_result()
        )

    assert result is True  # still suppressed (don't proceed to send)
    written.assert_not_awaited()  # no new row — deduped


@pytest.mark.asyncio
async def test_suppression_transition_from_other_state_writes_log(written):
    """Leaf was firing on its own (status:down), parent went down → one suppression row."""
    db = AsyncMock()
    parent_id = uuid4()
    check = _check(
        parent_id=parent_id, parent_check=SimpleNamespace(display_name="gateway")
    )
    last_row = SimpleNamespace(notification_hash="status:down")
    mappings = [_provider_mapping()]

    with (
        patch(
            "app.services.core.alert_evaluation_core_service.CheckResultCRUD.get_latest_result_for_check",
            AsyncMock(return_value=SimpleNamespace(success=False)),
        ),
        patch(
            "app.services.core.alert_evaluation_core_service.AlertCRUD.list_active_notif_mappings_for_alert",
            AsyncMock(return_value=mappings),
        ),
        patch(
            "app.services.core.alert_evaluation_core_service.AlertCRUD.get_last_notification_for_dedup",
            AsyncMock(return_value=last_row),
        ),
    ):
        result = await AlertEvaluationCoreService._handle_parent_suppression(
            db, _alert(), check, _check_result()
        )

    assert result is True
    logs = _logs(written)
    assert len(logs) == 1
    assert logs[0].notification_hash == "parent_down"


@pytest.mark.asyncio
async def test_validate_dependency_none_is_ok():
    db = AsyncMock()
    await CheckCoreService._validate_dependency(db, None, own_check_id=uuid4())


@pytest.mark.asyncio
async def test_validate_dependency_rejects_self_reference():
    db = AsyncMock()
    own = uuid4()
    with pytest.raises(ValidationException, match="cannot depend on itself"):
        await CheckCoreService._validate_dependency(db, own, own_check_id=own)


@pytest.mark.asyncio
async def test_validate_dependency_rejects_missing_parent():
    db = AsyncMock()
    with (
        patch(
            "app.services.core.check_core_service.CheckCRUD.get_by_id",
            AsyncMock(return_value=None),
        ),
        pytest.raises(ValidationException, match="not found"),
    ):
        await CheckCoreService._validate_dependency(db, uuid4(), own_check_id=uuid4())


@pytest.mark.asyncio
async def test_validate_dependency_rejects_two_level_chain():
    db = AsyncMock()
    parent = SimpleNamespace(id=uuid4(), depends_on_check_id=uuid4())
    with (
        patch(
            "app.services.core.check_core_service.CheckCRUD.get_by_id",
            AsyncMock(return_value=parent),
        ),
        pytest.raises(ValidationException, match="Single-level"),
    ):
        await CheckCoreService._validate_dependency(db, parent.id, own_check_id=uuid4())


@pytest.mark.asyncio
async def test_validate_dependency_accepts_valid_parent():
    db = AsyncMock()
    parent = SimpleNamespace(id=uuid4(), depends_on_check_id=None)
    with patch(
        "app.services.core.check_core_service.CheckCRUD.get_by_id",
        AsyncMock(return_value=parent),
    ):
        await CheckCoreService._validate_dependency(db, parent.id, own_check_id=uuid4())
