"""Test entity factories.

Create real model instances with sensible defaults. Tests call `db.add()`
explicitly — these are pure constructors, no session coupling.

Pattern mirrors luxwx/apps/backend/tests/fixtures/factories.py.

Usage:
    agent = make_agent()
    db.add(agent)
    await db.flush()

    check = make_check(agent_id=agent.id)
    db.add(check)
    await db.flush()
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from app.core.datetime_utils import utc_now
from app.models.agent_model import Agent
from app.models.alert_check_mapping_model import AlertCheckMapping
from app.models.alert_model import Alert
from app.models.alert_notification_mapping_model import AlertNotificationMapping
from app.models.check_model import Check
from app.models.check_result_model import CheckResult
from app.models.notification_log_model import NotificationLog
from app.models.notification_provider_model import NotificationProvider
from app.models.user_model import User
from app.services.core.auth_core_service import AuthCoreService


def make_agent(**overrides) -> Agent:
    """Create an Agent with sensible defaults (status=active so it counts as live).

    Note: real registration sets approval_status='pending'; tests that need
    that state should pass `approval_status='pending'` explicitly. Default is
    'active' because most tests want a usable agent without a separate approve
    step.
    """
    now = utc_now()
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "agent_name": f"test-agent-{uuid4().hex[:8]}",
        "agent_run_id": uuid4().hex,
        "first_seen": now,
        "last_seen": now,
        "hostname": "test-host",
        "ip_address": "127.0.0.1",
        "version": "1.0.0",
        "status": "online",
        "approval_status": "active",
        # Numeric stat fields default to 0 so AgentResponse serialization
        # (which requires non-null floats) doesn't choke on freshly-created
        # test agents. Real agents fill these in via the heartbeat path.
        "uptime_seconds": 0,
        "checks_total": 0,
        "checks_active": 0,
        "checks_executed_total": 0,
        "checks_succeeded_total": 0,
        "checks_failed_total": 0,
        "cpu_percent": 0.0,
        "memory_mb": 0,
        "queue_depth": 0,
    }
    defaults.update(overrides)
    return Agent(**defaults)


def make_check(*, agent_id, **overrides) -> Check:
    """Create a Check belonging to an agent."""
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "agent_id": agent_id,
        "display_name": f"test-check-{uuid4().hex[:8]}",
        "check_type": "ping",
        "target": "127.0.0.1",
        "interval_seconds": 60,
        "timeout_seconds": 5,
        "retry_interval_seconds": 30,
        "enabled": True,
        "assignment_mode": "manual",
    }
    defaults.update(overrides)
    return Check(**defaults)


def make_check_result(*, check_id, agent_id, **overrides) -> CheckResult:
    """Create a CheckResult. `success=True` by default."""
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "timestamp": utc_now(),
        "agent_id": agent_id,
        "check_id": check_id,
        "success": True,
        "latency_ms": 12.5,
    }
    defaults.update(overrides)
    return CheckResult(**defaults)


def make_check_result_batch(
    *,
    check_id,
    agent_id,
    count: int = 5,
    interval_seconds: int = 60,
    start_time: datetime | None = None,
    success: bool = True,
    **overrides,
) -> list[CheckResult]:
    """Create N check_results with sequential timestamps.

    Useful for tests that exercise time-series queries (history, rolling
    averages, consecutive-failures eval).
    """
    if start_time is None:
        start_time = utc_now() - timedelta(seconds=count * interval_seconds)
    results = []
    for i in range(count):
        results.append(
            make_check_result(
                check_id=check_id,
                agent_id=agent_id,
                timestamp=start_time + timedelta(seconds=i * interval_seconds),
                success=success,
                **overrides,
            )
        )
    return results


def make_user(**overrides) -> User:
    """Create a User. Default role=admin so tests that need authz pass with
    the default. Tests that exercise role gating should pass role=viewer/etc.
    """
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "username": f"user-{uuid4().hex[:8]}",
        "password_hash": AuthCoreService.hash_password("TestPass123!"),
        "role": "admin",
        "is_active": True,
        "failed_login_attempts": 0,
        "must_change_password": False,
    }
    defaults.update(overrides)
    return User(**defaults)


def make_notification_provider(**overrides) -> NotificationProvider:
    """Create a NotificationProvider (webhook by default — simplest config).

    Config matches what the WebhookNotificationProvider.validate_config expects
    (post_url is required).
    """
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "provider_type": "webhook",
        "friendly_name": f"test-provider-{uuid4().hex[:8]}",
        "config": {"post_url": "https://example.test/hook"},
        "is_enabled": True,
        "is_default_enabled": False,
    }
    defaults.update(overrides)
    return NotificationProvider(**defaults)


def make_alert(**overrides) -> Alert:
    """Create an Alert. Default trigger is `status_change` because it's the
    simplest to exercise in tests (just flip check_result.success).
    """
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "name": f"test-alert-{uuid4().hex[:8]}",
        "trigger_type": "status_change",
        "trigger_config": {"on_status": ["error"], "consecutive_failures": 1},
        "is_enabled": True,
        "is_global": False,
        "notify_on_recovery": True,
    }
    defaults.update(overrides)
    return Alert(**defaults)


def make_alert_check_mapping(*, alert_id, check_id, **overrides) -> AlertCheckMapping:
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "alert_id": alert_id,
        "check_id": check_id,
        "is_enabled": True,
    }
    defaults.update(overrides)
    return AlertCheckMapping(**defaults)


def make_alert_notification_mapping(
    *, alert_id, notification_provider_id, **overrides
) -> AlertNotificationMapping:
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "alert_id": alert_id,
        "notification_provider_id": notification_provider_id,
        "is_enabled": True,
    }
    defaults.update(overrides)
    return AlertNotificationMapping(**defaults)


def make_notification_log(
    *,
    alert_id,
    notification_provider_id,
    check_result_id,
    check_result_timestamp,
    **overrides,
) -> NotificationLog:
    """Create a NotificationLog. Default status='sent' as the common case."""
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "alert_id": alert_id,
        "notification_provider_id": notification_provider_id,
        "check_result_id": check_result_id,
        "check_result_timestamp": check_result_timestamp,
        "status": "sent",
        "sent_at": utc_now(),
        "is_resend": False,
        "resend_count": 0,
    }
    defaults.update(overrides)
    return NotificationLog(**defaults)


# ---------------------------------------------------------------------------
# Convenience helpers for common multi-entity setups
# ---------------------------------------------------------------------------


async def setup_agent_with_check(db, *, check_type: str = "ping", **check_overrides):
    """Create + flush an active agent and one check belonging to it.

    Returns (agent, check). Common scaffolding for CRUD/service tests where
    you need a check that's already wired to an agent in the DB.
    """
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(agent_id=agent.id, check_type=check_type, **check_overrides)
    db.add(check)
    await db.flush()
    return agent, check
