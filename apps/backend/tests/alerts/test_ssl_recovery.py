"""Unit tests for AlertCoreService._evaluate_ssl_cert_expiry recovery branch.

SSL alerts didn't fire on cert renewal pre-LUXSWIRL-146. The recovery branch
returns True when the cert is currently safe AND the last notification for this
alert+check was in-band (`ssl:lte:*`), so operators see a recovery signal.

Gated by `alert.notify_on_recovery` to avoid an extra DB lookup when the
operator opted out of recovery notifications.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.services.core.alert_core_service import AlertCoreService

pytestmark = pytest.mark.pure  # all tests in this module are pure-logic


def _alert(*, notify_on_recovery: bool = True):
    return SimpleNamespace(id=uuid4(), notify_on_recovery=notify_on_recovery)


def _check():
    return SimpleNamespace(id=uuid4())


def _result(days_until: int | None):
    cert = {} if days_until is None else {"days_until_expiration": days_until}
    r = SimpleNamespace()
    r.get_metrics = lambda: {"response": {"ssl_certificate": cert}}
    return r


def _result_no_metrics():
    r = SimpleNamespace()
    r.get_metrics = lambda: None
    return r


def _result_no_ssl():
    r = SimpleNamespace()
    r.get_metrics = lambda: {"response": {}}
    return r


def _prior(hash: str | None):
    return SimpleNamespace(notification_hash=hash)


THRESHOLDS = {"days_thresholds": [7, 14, 30]}


@pytest.mark.asyncio
async def test_in_danger_zone_fires_without_db_lookup():
    """Cert at 5 days → in-band → fire. No need to check prior state."""
    db = AsyncMock()
    # Patch get_last_notification_for_dedup to assert it's NOT called
    with patch(
        "app.services.core.alert_core_service.AlertCRUD.get_last_notification_for_dedup",
        AsyncMock(),
    ) as m:
        r = await AlertCoreService._evaluate_ssl_cert_expiry(
            db, _alert(), _check(), _result(5), THRESHOLDS
        )
    assert r is True
    m.assert_not_called()


@pytest.mark.asyncio
async def test_safe_with_no_prior_does_not_fire():
    """Cert at 60 days, no notification history → no fire."""
    db = AsyncMock()
    with patch(
        "app.services.core.alert_core_service.AlertCRUD.get_last_notification_for_dedup",
        AsyncMock(return_value=None),
    ):
        r = await AlertCoreService._evaluate_ssl_cert_expiry(
            db, _alert(), _check(), _result(60), THRESHOLDS
        )
    assert r is False


@pytest.mark.asyncio
async def test_renewal_from_band_fires_recovery():
    """Last notification was ssl:lte:7, cert now at 90 days → fire (recovery)."""
    db = AsyncMock()
    with patch(
        "app.services.core.alert_core_service.AlertCRUD.get_last_notification_for_dedup",
        AsyncMock(return_value=_prior(hash="ssl:lte:7")),
    ):
        r = await AlertCoreService._evaluate_ssl_cert_expiry(
            db, _alert(), _check(), _result(90), THRESHOLDS
        )
    assert r is True


@pytest.mark.asyncio
async def test_renewal_from_widest_band():
    """Last notification was ssl:lte:30 (widest band), cert now safe → fire."""
    db = AsyncMock()
    with patch(
        "app.services.core.alert_core_service.AlertCRUD.get_last_notification_for_dedup",
        AsyncMock(return_value=_prior(hash="ssl:lte:30")),
    ):
        r = await AlertCoreService._evaluate_ssl_cert_expiry(
            db, _alert(), _check(), _result(90), THRESHOLDS
        )
    assert r is True


@pytest.mark.asyncio
async def test_recovery_disabled_skips_db_lookup():
    """notify_on_recovery=False → safe cert never fires, no DB query."""
    db = AsyncMock()
    with patch(
        "app.services.core.alert_core_service.AlertCRUD.get_last_notification_for_dedup",
        AsyncMock(),
    ) as m:
        r = await AlertCoreService._evaluate_ssl_cert_expiry(
            db, _alert(notify_on_recovery=False), _check(), _result(90), THRESHOLDS
        )
    assert r is False
    m.assert_not_called()


@pytest.mark.asyncio
async def test_already_fired_recovery_does_not_refire():
    """Last notification was ssl:ok — recovery already fired, don't refire."""
    db = AsyncMock()
    with patch(
        "app.services.core.alert_core_service.AlertCRUD.get_last_notification_for_dedup",
        AsyncMock(return_value=_prior(hash="ssl:ok")),
    ):
        r = await AlertCoreService._evaluate_ssl_cert_expiry(
            db, _alert(), _check(), _result(90), THRESHOLDS
        )
    assert r is False


@pytest.mark.asyncio
async def test_prior_row_without_hash_does_not_trigger_recovery():
    """Legacy row (notification_hash=None) → don't infer recovery state."""
    db = AsyncMock()
    with patch(
        "app.services.core.alert_core_service.AlertCRUD.get_last_notification_for_dedup",
        AsyncMock(return_value=_prior(hash=None)),
    ):
        r = await AlertCoreService._evaluate_ssl_cert_expiry(
            db, _alert(), _check(), _result(90), THRESHOLDS
        )
    assert r is False


@pytest.mark.asyncio
async def test_no_metrics_does_not_fire():
    """check_result has no metrics blob — not applicable, no fire."""
    db = AsyncMock()
    r = await AlertCoreService._evaluate_ssl_cert_expiry(
        db, _alert(), _check(), _result_no_metrics(), THRESHOLDS
    )
    assert r is False


@pytest.mark.asyncio
async def test_no_ssl_cert_in_metrics_does_not_fire():
    """Non-HTTPS check (no ssl_certificate key) — not applicable."""
    db = AsyncMock()
    r = await AlertCoreService._evaluate_ssl_cert_expiry(
        db, _alert(), _check(), _result_no_ssl(), THRESHOLDS
    )
    assert r is False


@pytest.mark.asyncio
async def test_days_until_missing_does_not_fire():
    db = AsyncMock()
    r = await AlertCoreService._evaluate_ssl_cert_expiry(
        db, _alert(), _check(), _result(None), THRESHOLDS
    )
    assert r is False


@pytest.mark.asyncio
async def test_legacy_single_threshold_config():
    """Old `days_threshold` singleton still works (no days_thresholds array)."""
    db = AsyncMock()
    r = await AlertCoreService._evaluate_ssl_cert_expiry(
        db, _alert(), _check(), _result(20), {"days_threshold": 30}
    )
    assert r is True
