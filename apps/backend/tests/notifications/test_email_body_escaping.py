"""Alert emails escape every value they carry.

The HTML body was an f-string, so a check name, a target or the monitored
service's own error text went into the email as markup. It is now a template
with autoescape on.
"""

from __future__ import annotations

import pytest
from app.notifications.providers.base import NotificationContext
from app.notifications.providers.email import EmailNotificationProvider

pytestmark = pytest.mark.unit

CONFIG = {
    "hostname": "smtp.example.com",
    "port": 587,
    "from_email": "alerts@example.com",
    "to_email": "ops@example.com",
}


def _body(
    *,
    check_name: str = "api-health",
    target: str = "https://api.example.com",
    error_message: str | None = None,
) -> str:
    context = NotificationContext(
        check_name=check_name,
        check_type="http",
        target=target,
        agent_id=None,
        status="error",
        success=False,
        latency_ms=1234.5,
        error_message=error_message,
        http_status_code=503,
    )
    return EmailNotificationProvider(CONFIG)._build_html_body(context)


def test_hostile_values_are_escaped() -> None:
    body = _body(
        check_name="<img src=x onerror=alert(1)>",
        target='https://evil.example/"><script>x()</script>',
        error_message="<b>upstream</b> & friends",
    )

    assert "<img src=x" not in body
    assert "<script>" not in body
    assert "&lt;img src=x onerror=alert(1)&gt;" in body
    assert "&lt;b&gt;upstream&lt;/b&gt; &amp; friends" in body


def test_the_body_still_reports_the_check() -> None:
    body = _body(error_message="connection refused")

    assert "✗ DOWN api-health" in body
    assert "1234.50ms" in body
    assert "connection refused" in body
    assert "503" in body
