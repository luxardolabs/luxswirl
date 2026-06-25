"""Unit tests for AlertCoreService._should_send_notification and SendDecision.

Covers the dedup state machine that gates notification delivery:
- Snooze short-circuit
- First-contact (no prior row) → fresh fire
- State transitions (hash key changed) → fresh fire, counter resets
- Same state + no resend interval → skip
- Same state + resend interval elapsed → resend, counter increments
- Same state + max_resends reached → skip, counter preserved
- Legacy fallback path (prior row without notification_hash)

See LUXSWIRL-145, LUXSWIRL-147, LUXSWIRL-149.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.services.core.alert_core_service import AlertCoreService, SendDecision

pytestmark = pytest.mark.pure  # all tests in this module are pure-logic


def _alert(
    *,
    resend_interval_minutes: int | None = None,
    max_resends: int | None = None,
    trigger_type: str = "status_change",
    trigger_config: dict | None = None,
):
    return SimpleNamespace(
        id=uuid4(),
        name="test-alert",
        resend_interval_minutes=resend_interval_minutes,
        max_resends=max_resends,
        trigger_type=trigger_type,
        trigger_config=trigger_config or {},
    )


def _check():
    return SimpleNamespace(id=uuid4(), display_name="test-check")


def _result(success: bool = False):
    return SimpleNamespace(id=uuid4(), success=success)


def _prior(
    *,
    hash: str | None,
    sent_at: datetime | None = None,
    resend_count: int = 0,
    check_result_id=None,
):
    return SimpleNamespace(
        notification_hash=hash,
        sent_at=sent_at or datetime.now(UTC),
        resend_count=resend_count,
        check_result_id=check_result_id or uuid4(),
    )


# ---------------------------------------------------------------------------
# Snooze
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snoozed_returns_skip():
    db = AsyncMock()
    mapping = SimpleNamespace(snoozed_until=datetime.now(UTC) + timedelta(hours=1))
    with patch(
        "app.services.core.alert_core_service.AlertCRUD.get_check_mapping",
        AsyncMock(return_value=mapping),
    ):
        d = await AlertCoreService._should_send_notification(
            db, _alert(), _check(), _result(), is_recovery=False
        )
    assert d == SendDecision(send=False, is_resend=False, resend_count=0)


@pytest.mark.asyncio
async def test_expired_snooze_proceeds_to_dedup():
    db = AsyncMock()
    mapping = SimpleNamespace(snoozed_until=datetime.now(UTC) - timedelta(hours=1))
    with (
        patch(
            "app.services.core.alert_core_service.AlertCRUD.get_check_mapping",
            AsyncMock(return_value=mapping),
        ),
        patch(
            "app.services.core.alert_core_service.AlertCRUD.get_last_notification_for_dedup",
            AsyncMock(return_value=None),
        ),
    ):
        d = await AlertCoreService._should_send_notification(
            db, _alert(), _check(), _result(), is_recovery=False
        )
    # Expired snooze + no prior → fresh fire
    assert d == SendDecision(send=True, is_resend=False, resend_count=0)


# ---------------------------------------------------------------------------
# First contact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_prior_notification_sends_fresh():
    db = AsyncMock()
    with (
        patch(
            "app.services.core.alert_core_service.AlertCRUD.get_check_mapping",
            AsyncMock(return_value=None),
        ),
        patch(
            "app.services.core.alert_core_service.AlertCRUD.get_last_notification_for_dedup",
            AsyncMock(return_value=None),
        ),
    ):
        d = await AlertCoreService._should_send_notification(
            db, _alert(), _check(), _result(), is_recovery=False
        )
    assert d == SendDecision(send=True, is_resend=False, resend_count=0)


# ---------------------------------------------------------------------------
# Hash-based dedup (post-LUXSWIRL-147)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_transition_resets_resend_counter():
    """Prior row was status:up, current is status:down → fresh fire."""
    db = AsyncMock()
    prior = _prior(hash="status:up", resend_count=5)
    with (
        patch(
            "app.services.core.alert_core_service.AlertCRUD.get_check_mapping",
            AsyncMock(return_value=None),
        ),
        patch(
            "app.services.core.alert_core_service.AlertCRUD.get_last_notification_for_dedup",
            AsyncMock(return_value=prior),
        ),
    ):
        d = await AlertCoreService._should_send_notification(
            db,
            _alert(resend_interval_minutes=5),
            _check(),
            _result(success=False),
            is_recovery=False,
        )
    assert d == SendDecision(send=True, is_resend=False, resend_count=0)


@pytest.mark.asyncio
async def test_same_state_no_resend_configured_skips():
    db = AsyncMock()
    prior = _prior(hash="status:down")
    with (
        patch(
            "app.services.core.alert_core_service.AlertCRUD.get_check_mapping",
            AsyncMock(return_value=None),
        ),
        patch(
            "app.services.core.alert_core_service.AlertCRUD.get_last_notification_for_dedup",
            AsyncMock(return_value=prior),
        ),
    ):
        d = await AlertCoreService._should_send_notification(
            db,
            _alert(resend_interval_minutes=None),
            _check(),
            _result(success=False),
            is_recovery=False,
        )
    assert d == SendDecision(send=False, is_resend=False, resend_count=0)


@pytest.mark.asyncio
async def test_same_state_within_resend_interval_skips():
    db = AsyncMock()
    prior = _prior(
        hash="status:down",
        sent_at=datetime.now(UTC) - timedelta(minutes=2),
        resend_count=1,
    )
    with (
        patch(
            "app.services.core.alert_core_service.AlertCRUD.get_check_mapping",
            AsyncMock(return_value=None),
        ),
        patch(
            "app.services.core.alert_core_service.AlertCRUD.get_last_notification_for_dedup",
            AsyncMock(return_value=prior),
        ),
    ):
        d = await AlertCoreService._should_send_notification(
            db,
            _alert(resend_interval_minutes=5),
            _check(),
            _result(success=False),
            is_recovery=False,
        )
    assert d == SendDecision(send=False, is_resend=False, resend_count=0)


@pytest.mark.asyncio
async def test_same_state_resend_interval_elapsed_increments_counter():
    db = AsyncMock()
    prior = _prior(
        hash="status:down",
        sent_at=datetime.now(UTC) - timedelta(minutes=10),
        resend_count=2,
    )
    with (
        patch(
            "app.services.core.alert_core_service.AlertCRUD.get_check_mapping",
            AsyncMock(return_value=None),
        ),
        patch(
            "app.services.core.alert_core_service.AlertCRUD.get_last_notification_for_dedup",
            AsyncMock(return_value=prior),
        ),
    ):
        d = await AlertCoreService._should_send_notification(
            db,
            _alert(resend_interval_minutes=5),
            _check(),
            _result(success=False),
            is_recovery=False,
        )
    assert d == SendDecision(send=True, is_resend=True, resend_count=3)


@pytest.mark.asyncio
async def test_max_resends_reached_skips_but_marks_is_resend():
    """max_resends=3 with prior_count=3 → skip, but record that we considered it a resend."""
    db = AsyncMock()
    prior = _prior(
        hash="status:down",
        sent_at=datetime.now(UTC) - timedelta(minutes=10),
        resend_count=3,
    )
    with (
        patch(
            "app.services.core.alert_core_service.AlertCRUD.get_check_mapping",
            AsyncMock(return_value=None),
        ),
        patch(
            "app.services.core.alert_core_service.AlertCRUD.get_last_notification_for_dedup",
            AsyncMock(return_value=prior),
        ),
    ):
        d = await AlertCoreService._should_send_notification(
            db,
            _alert(resend_interval_minutes=5, max_resends=3),
            _check(),
            _result(success=False),
            is_recovery=False,
        )
    assert d == SendDecision(send=False, is_resend=True, resend_count=3)


@pytest.mark.asyncio
async def test_max_resends_not_yet_reached_proceeds():
    db = AsyncMock()
    prior = _prior(
        hash="status:down",
        sent_at=datetime.now(UTC) - timedelta(minutes=10),
        resend_count=2,
    )
    with (
        patch(
            "app.services.core.alert_core_service.AlertCRUD.get_check_mapping",
            AsyncMock(return_value=None),
        ),
        patch(
            "app.services.core.alert_core_service.AlertCRUD.get_last_notification_for_dedup",
            AsyncMock(return_value=prior),
        ),
    ):
        d = await AlertCoreService._should_send_notification(
            db,
            _alert(resend_interval_minutes=5, max_resends=5),
            _check(),
            _result(success=False),
            is_recovery=False,
        )
    assert d == SendDecision(send=True, is_resend=True, resend_count=3)


# ---------------------------------------------------------------------------
# Legacy fallback (prior row predates LUXSWIRL-147 — no hash recorded)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_prior_without_hash_falls_back_to_success_comparison():
    """Prior row has notification_hash=None → compare check_result.success."""
    db = AsyncMock()
    prior_check_result = SimpleNamespace(success=True)  # was up
    prior = _prior(hash=None)
    with (
        patch(
            "app.services.core.alert_core_service.AlertCRUD.get_check_mapping",
            AsyncMock(return_value=None),
        ),
        patch(
            "app.services.core.alert_core_service.AlertCRUD.get_last_notification_for_dedup",
            AsyncMock(return_value=prior),
        ),
        patch(
            "app.services.core.alert_core_service.AlertCRUD.get_check_result_by_id",
            AsyncMock(return_value=prior_check_result),
        ),
    ):
        # Current is down → success changed → fresh fire
        d = await AlertCoreService._should_send_notification(
            db,
            _alert(),
            _check(),
            _result(success=False),
            is_recovery=False,
        )
    assert d == SendDecision(send=True, is_resend=False, resend_count=0)


@pytest.mark.asyncio
async def test_legacy_prior_without_hash_same_success_no_resend_skips():
    db = AsyncMock()
    prior_check_result = SimpleNamespace(success=False)
    prior = _prior(hash=None)
    with (
        patch(
            "app.services.core.alert_core_service.AlertCRUD.get_check_mapping",
            AsyncMock(return_value=None),
        ),
        patch(
            "app.services.core.alert_core_service.AlertCRUD.get_last_notification_for_dedup",
            AsyncMock(return_value=prior),
        ),
        patch(
            "app.services.core.alert_core_service.AlertCRUD.get_check_result_by_id",
            AsyncMock(return_value=prior_check_result),
        ),
    ):
        d = await AlertCoreService._should_send_notification(
            db,
            _alert(resend_interval_minutes=None),
            _check(),
            _result(success=False),
            is_recovery=False,
        )
    assert d == SendDecision(send=False, is_resend=False, resend_count=0)


@pytest.mark.asyncio
async def test_legacy_prior_check_result_missing_treats_as_fresh():
    """If the prior row's check_result was deleted (retention), don't crash — send."""
    db = AsyncMock()
    prior = _prior(hash=None)
    with (
        patch(
            "app.services.core.alert_core_service.AlertCRUD.get_check_mapping",
            AsyncMock(return_value=None),
        ),
        patch(
            "app.services.core.alert_core_service.AlertCRUD.get_last_notification_for_dedup",
            AsyncMock(return_value=prior),
        ),
        patch(
            "app.services.core.alert_core_service.AlertCRUD.get_check_result_by_id",
            AsyncMock(return_value=None),
        ),
    ):
        d = await AlertCoreService._should_send_notification(
            db,
            _alert(),
            _check(),
            _result(success=False),
            is_recovery=False,
        )
    assert d == SendDecision(send=True, is_resend=False, resend_count=0)


# ---------------------------------------------------------------------------
# SendDecision dataclass
# ---------------------------------------------------------------------------


class TestSendDecision:
    def test_is_frozen(self):
        d = SendDecision(send=True, is_resend=False, resend_count=0)
        with pytest.raises((AttributeError, Exception)):
            d.send = False  # type: ignore[misc]

    def test_equality(self):
        a = SendDecision(send=True, is_resend=True, resend_count=2)
        b = SendDecision(send=True, is_resend=True, resend_count=2)
        c = SendDecision(send=True, is_resend=False, resend_count=2)
        assert a == b
        assert a != c
