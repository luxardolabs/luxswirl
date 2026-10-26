"""
The shape of an alert's `trigger_config`, per trigger type.

Alert evaluation reads these keys and compares their values, so a value of the
wrong type (a count sent as a string) would raise there, for every alert on
the check. A config the admin API stores is validated against its trigger type
here first, and stored in this normal form.
"""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.liveness import MAX_MISSED_INTERVALS
from app.models.enum_model import AlertTriggerType, CheckType

_STRICT = ConfigDict(extra="forbid", strict=True)


class CheckFilters(BaseModel):
    """Which checks a global alert reaches; every filter given must match."""

    model_config = ConfigDict(extra="forbid")

    agent_ids: list[UUID] | None = Field(None, max_length=1000)
    check_types: list[CheckType] | None = Field(None, max_length=50)
    check_names: list[str] | None = Field(None, max_length=100)
    tags: list[str] | None = Field(None, max_length=100)


class StatusChangeConfig(BaseModel):
    """`status_change` and `repeated_failure`: fire on a status after N in a row."""

    model_config = _STRICT

    on_status: list[Literal["error", "success"]] = Field(["error"], min_length=1)
    consecutive_failures: int = Field(1, ge=1, le=100)
    check_filters: CheckFilters | None = None


class ThresholdConfig(BaseModel):
    """`threshold`: fire when a metric crosses a value."""

    model_config = _STRICT

    metric: Literal["latency_ms"] = "latency_ms"
    operator: Literal[">", ">=", "<", "<=", "=="] = ">"
    value: float
    check_filters: CheckFilters | None = None


class SslCertExpiryConfig(BaseModel):
    """`ssl_cert_expiry`: fire as a certificate nears expiry, once per threshold."""

    model_config = _STRICT

    days_thresholds: list[int] = Field([30], min_length=1, max_length=10)
    check_filters: CheckFilters | None = None


class SilenceConfig(BaseModel):
    """`no_data` and `agent_offline`: fire after N missed intervals."""

    model_config = _STRICT

    missed_intervals: int = Field(3, ge=1, le=MAX_MISSED_INTERVALS)
    check_filters: CheckFilters | None = None


_BY_TYPE: dict[AlertTriggerType, type[BaseModel]] = {
    AlertTriggerType.STATUS_CHANGE: StatusChangeConfig,
    AlertTriggerType.REPEATED_FAILURE: StatusChangeConfig,
    AlertTriggerType.THRESHOLD: ThresholdConfig,
    AlertTriggerType.SSL_CERT_EXPIRY: SslCertExpiryConfig,
    AlertTriggerType.NO_DATA: SilenceConfig,
    AlertTriggerType.AGENT_OFFLINE: SilenceConfig,
}


def normalized_trigger_config(
    trigger_type: AlertTriggerType, config: dict[str, Any]
) -> dict[str, Any]:
    """`config` validated against `trigger_type`, in the form evaluation reads.

    Raises:
        ValueError: The config does not fit the trigger type; the message names
            the field and the reason.
    """
    try:
        parsed = _BY_TYPE[trigger_type].model_validate(config)
    except ValidationError as e:
        first = e.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "trigger_config"
        raise ValueError(
            f"trigger_config for {trigger_type.value}: {where}: {first['msg']}"
        ) from None
    if isinstance(parsed, SslCertExpiryConfig) and any(
        not 1 <= days <= 365 for days in parsed.days_thresholds
    ):
        raise ValueError(
            "trigger_config for ssl_cert_expiry: days_thresholds: each from 1 to 365"
        )
    return parsed.model_dump(mode="json", exclude_none=True)
