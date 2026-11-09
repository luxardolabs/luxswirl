"""Text an input sets has a length limit at its schema, and every reader pays for it.

A result's `metrics`, a notification provider's `post_url`, and an update's
`target` and `description` were taken at any length: a 262,144-character
metrics object and a 65,535-character URL were stored, and an update's long
target reached the column, which refused it as a database error.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from app.notifications.providers.homeassistant import HomeAssistantNotificationProvider
from app.notifications.providers.webhook import WebhookNotificationProvider
from app.schemas.check_result_schema import MAX_METRICS_CHARS, CheckResultCreate
from app.schemas.check_schema import CheckUpdate
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def _result(metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": datetime.now(UTC),
        "success": True,
        "display_name": "c",
        "check_type": "http",
        "target": "https://example.com",
        "metrics": metrics,
    }


def test_metrics_past_the_limit_are_refused() -> None:
    with pytest.raises(ValidationError, match="metrics"):
        CheckResultCreate(**_result({"blob": "x" * MAX_METRICS_CHARS}))


def test_metrics_spread_over_many_keys_are_measured_whole() -> None:
    many = {f"k{n}": "x" * 1000 for n in range(MAX_METRICS_CHARS // 1000 + 1)}

    with pytest.raises(ValidationError, match="metrics"):
        CheckResultCreate(**_result(many))


def test_metrics_under_the_limit_are_kept() -> None:
    metrics = {"response": {"body": "x" * (MAX_METRICS_CHARS - 100)}}

    assert CheckResultCreate(**_result(metrics)).metrics == metrics


@pytest.mark.parametrize(
    "provider",
    [WebhookNotificationProvider, HomeAssistantNotificationProvider],
    ids=lambda p: p.__name__,
)
def test_a_provider_url_past_the_limit_is_refused(provider: type) -> None:
    url = "https://hooks.example.com/api/webhook/" + "a" * 65_535

    with pytest.raises(ValueError, match="2048"):
        provider({"post_url": url})


@pytest.mark.parametrize(
    "provider",
    [WebhookNotificationProvider, HomeAssistantNotificationProvider],
    ids=lambda p: p.__name__,
)
def test_a_provider_url_that_is_not_text_is_refused(provider: type) -> None:
    with pytest.raises(ValueError, match="post_url"):
        provider({"post_url": ["https://hooks.example.com/api/webhook/x"]})


def test_an_update_target_has_the_create_limit() -> None:
    with pytest.raises(ValidationError, match="target"):
        CheckUpdate(target="https://example.com/" + "a" * 700)
    with pytest.raises(ValidationError, match="target"):
        CheckUpdate(target="")


def test_an_update_description_has_the_create_limit() -> None:
    with pytest.raises(ValidationError, match="description"):
        CheckUpdate(description="d" * 1001)
