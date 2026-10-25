"""An SSL Certificate Expiry alert says what it found, including an unreadable expiry.

A certificate whose expiry cannot be read fires the alert once (state
`ssl:unknown`), so broken expiry tracking reaches the operator instead of
reading as healthy. Every SSL notification carries a `detail` line saying what
was found (unreadable, days left, expired, renewed), which every provider
shows even though the check itself succeeded. Only a certificate back out of
every band is a recovery; an in-band warning on a successful check is not.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from app.notifications.providers.base import NotificationContext
from app.notifications.providers.email import EmailNotificationProvider
from app.notifications.providers.homeassistant import HomeAssistantNotificationProvider
from app.notifications.providers.webhook import WebhookNotificationProvider
from app.services.core.alert_evaluation_core_service import AlertEvaluationCoreService

pytestmark = pytest.mark.unit

THRESHOLDS = {"days_thresholds": [7, 14, 30]}
UNREADABLE = {
    "expiration_date": "sometime next spring",
    "state": "unknown",
    "days_until_expiration": None,
    "expired": False,
    "expires_soon": False,
    "expires_critical": False,
}
DEDUP = (
    "app.services.core.alert_evaluation_core_service."
    "AlertCRUD.get_last_notification_for_dedup"
)


def _alert(**overrides: Any) -> SimpleNamespace:
    fields: dict[str, Any] = {
        "id": uuid4(),
        "name": "cert watch",
        "description": None,
        "trigger_type": "ssl_cert_expiry",
        "trigger_config": THRESHOLDS,
        "notify_on_recovery": True,
        "custom_subject": None,
        "custom_message": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _result(cert: dict[str, Any], *, success: bool = True) -> SimpleNamespace:
    r = SimpleNamespace(
        success=success,
        agent=None,
        latency_ms=12.0,
        timestamp=datetime(2026, 10, 25, tzinfo=UTC),
        error=None,
        error_type=None,
        http_status_code=200,
    )
    r.get_metrics = lambda: {"response": {"ssl_certificate": cert}}
    return r


def _cert(days: int) -> dict[str, Any]:
    return {
        "expiration_date": "Jan 15 23:59:59 2027 GMT",
        "days_until_expiration": days,
    }


def _check() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(), display_name="shop", check_type="http", target="https://shop.test"
    )


# --- when it fires -------------------------------------------------------------


async def test_an_unreadable_expiry_fires() -> None:
    fired = await AlertEvaluationCoreService._evaluate_ssl_cert_expiry(
        AsyncMock(), _alert(), _check(), _result(UNREADABLE), THRESHOLDS
    )

    assert fired is True


def test_an_unreadable_expiry_is_its_own_state() -> None:
    assert (
        AlertEvaluationCoreService._compute_alert_state_key(
            _alert(), _result(UNREADABLE)
        )
        == "ssl:unknown"
    )


async def test_a_readable_safe_expiry_after_an_unreadable_one_fires_recovery() -> None:
    with patch(
        DEDUP, AsyncMock(return_value=SimpleNamespace(notification_hash="ssl:unknown"))
    ):
        fired = await AlertEvaluationCoreService._evaluate_ssl_cert_expiry(
            AsyncMock(), _alert(), _check(), _result(_cert(90)), THRESHOLDS
        )

    assert fired is True


# --- what it says ----------------------------------------------------------------


def _context(cert: dict[str, Any], **alert_overrides: Any) -> NotificationContext:
    alert = _alert(**alert_overrides)
    result = _result(cert)
    return AlertEvaluationCoreService._build_notification_context(
        alert,
        _check(),
        result,
        is_recovery=AlertEvaluationCoreService._is_recovery(alert, result),
    )


def test_an_unreadable_expiry_says_so_and_quotes_the_date() -> None:
    context = _context(UNREADABLE)

    assert context.detail is not None
    assert "could not be read" in context.detail
    assert "sometime next spring" in context.detail
    assert context.is_recovery is False


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (5, "expires in 5 days"),
        (1, "expires in 1 day"),
        (0, "expires today"),
        (-3, "expired 3 days ago"),
    ],
)
def test_an_in_band_certificate_says_how_long_is_left(days: int, expected: str) -> None:
    context = _context(_cert(days))

    assert context.detail is not None and expected in context.detail
    assert context.is_recovery is False


def test_a_renewed_certificate_is_a_recovery_and_says_so() -> None:
    context = _context(_cert(90))

    assert context.is_recovery is True
    assert context.detail is not None and "90 days" in context.detail


def test_no_recovery_when_the_alert_does_not_want_one() -> None:
    assert _context(_cert(90), notify_on_recovery=False).is_recovery is False


def test_other_triggers_carry_no_detail() -> None:
    context = _context(_cert(5), trigger_type="status_change")

    assert context.detail is None


# --- every provider shows it ------------------------------------------------------


def _detailed() -> NotificationContext:
    return NotificationContext(
        check_name="shop",
        check_type="http",
        target="https://shop.test",
        agent_id=None,
        status="success",
        success=True,
        detail="SSL certificate expiry could not be read",
    )


def test_the_email_shows_the_detail_on_a_successful_check() -> None:
    provider = EmailNotificationProvider(
        {
            "hostname": "smtp.example.com",
            "port": 587,
            "from_email": "a@example.com",
            "to_email": "b@example.com",
        }
    )

    assert "SSL certificate expiry could not be read" in provider._build_html_body(
        _detailed()
    )
    assert "could not be read" in provider.format_message(_detailed())


def test_the_webhook_carries_the_detail() -> None:
    provider = WebhookNotificationProvider({"post_url": "https://hooks.example/x"})

    assert provider._build_payload(_detailed())["detail"] == (
        "SSL certificate expiry could not be read"
    )


def test_home_assistant_carries_the_detail() -> None:
    provider = HomeAssistantNotificationProvider({"post_url": "https://ha.example/x"})

    payload = provider._build_ha_payload(_detailed())

    assert "SSL certificate expiry could not be read" in str(payload)


def test_a_custom_template_can_use_the_detail() -> None:
    provider = WebhookNotificationProvider({"post_url": "https://hooks.example/x"})

    assert (
        provider._substitute_variables("{{NAME}}: {{DETAIL}}", _detailed())
        == "shop: SSL certificate expiry could not be read"
    )
