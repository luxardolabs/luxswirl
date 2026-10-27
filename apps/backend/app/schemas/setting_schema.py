"""
Setting schemas - Pydantic models for settings validation and serialization.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from app.models.enum_model import HttpMethod, SettingCategory
from app.models.setting_model import value_type_of

# A setting's stored value. The closed set the storage format documents ("int, float,
# bool, string, list, dict") — written as a real union so mypy can see it, rather than
# `Any`, which is the type-check dodge fw.no_any_typed_service_boundary exists to catch.
type SettingScalar = str | int | float | bool | list[Any] | dict[str, Any] | None


class SettingResponse(BaseModel):
    """Schema for setting response."""

    id: UUID
    key: str
    category: SettingCategory
    subcategory: str | None = None
    value: dict[str, Any]  # JSONB storage
    default_value: dict[str, Any]
    display_name: str
    description: str | None
    validation: dict[str, Any] | None
    created_at: Any
    updated_at: Any

    @field_validator("value", "default_value", mode="before")
    @classmethod
    def _wrapped(cls, stored: object) -> object:
        """A row written outside the app may hold the bare value; read it in the
        {"value", "type"} form the app writes."""
        if isinstance(stored, dict) and "value" in stored:
            return stored
        return {"value": stored, "type": value_type_of(stored)}

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "id": "123e4567-e89b-12d3-a456-426614174000",
                "key": "check.default_interval",
                "category": "check",
                "value": {"value": 60, "type": "int"},
                "default_value": {"value": 60, "type": "int"},
                "display_name": "Default Check Interval",
                "description": "Default interval in seconds between check executions",
                "validation": {"min": 10, "max": 86400},
                "created_at": "2025-11-07T10:00:00Z",
                "updated_at": "2025-11-07T10:00:00Z",
            }
        },
    )


class NetworkProtection(BaseModel):
    """The SSRF settings (Settings > Security > Network Protection)."""

    block_cloud_metadata: bool
    block_private_networks: bool


class CheckDefaults(BaseModel):
    """Schema for check defaults bundle."""

    interval_seconds: int
    timeout_seconds: int
    retry_attempts: int
    retry_interval_seconds: int
    expected_status: int
    verify_ssl: bool
    http_method: HttpMethod

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "interval_seconds": 60,
                "timeout_seconds": 10,
                "retry_attempts": 2,
                "retry_interval_seconds": 30,
                "expected_status": 200,
                "verify_ssl": False,
                "http_method": "GET",
            }
        }
    )


class AlertDefaults(BaseModel):
    """Schema for alert defaults bundle."""

    consecutive_failures: int
    notify_on_recovery: bool
    latency_threshold_ms: int
    ssl_cert_warning_days: int
    ssl_cert_critical_days: int

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "consecutive_failures": 1,
                "notify_on_recovery": True,
                "latency_threshold_ms": 1000,
                "ssl_cert_warning_days": 30,
                "ssl_cert_critical_days": 14,
            }
        }
    )


class DatabaseGrowthPoint(BaseModel):
    """One bucket of the database-growth chart (storage by table category, MB)."""

    timestamp: str | None
    results_mb: float
    artifacts_mb: float
    checks_mb: float
    agent_metrics_mb: float
    total_mb: float


class DatabaseGrowthResponse(BaseModel):
    """Response for the database-growth chart endpoint."""

    data: list[DatabaseGrowthPoint]


@dataclass(frozen=True)
class SettingUrls:
    """Finished update/reset hrefs for one setting.

    A dataclass, not a dict: Jinja resolves `x.update` to dict.update — the bound
    METHOD — before it ever tries the key, so a dict silently renders
    "<built-in method update of dict object …>" into the form's hx-post.
    """

    update: str
    reset: str


@dataclass(frozen=True)
class MetricsCardUrls:
    """Finished hrefs for the Prometheus metrics card's controls
    (fw.urls_from_route_names)."""

    toggle_enabled: str
    toggle_auth: str
    update_timeout: str
    generate_token: str


class SettingRow(SettingResponse):
    """A setting as its card renders it, with its update/reset links."""

    urls: SettingUrls
