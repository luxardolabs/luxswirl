"""A latency threshold alert's comparison: its value, and whether a latency
meets it."""

from typing import Any


def threshold_value(config: dict[str, Any] | None) -> float | None:
    """A latency threshold's value as a number, or None when it has none it
    can compare (another metric, no value, not a number)."""
    config = config or {}
    if config.get("metric", "latency_ms") != "latency_ms":
        return None
    value = config.get("value")
    # trigger_config is free-form JSON: a number or a numeric string compares.
    if value is None or not isinstance(value, int | float | str):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def latency_breaches(latency_ms: float | None, config: dict[str, Any] | None) -> bool:
    """Whether `latency_ms` meets the threshold's comparison (e.g. > 1000)."""
    threshold = threshold_value(config)
    if threshold is None or latency_ms is None:
        return False
    current = float(latency_ms)
    operator = (config or {}).get("operator", ">")
    if operator == ">":
        return current > threshold
    if operator == ">=":
        return current >= threshold
    if operator == "<":
        return current < threshold
    if operator == "<=":
        return current <= threshold
    if operator == "==":
        return current == threshold
    return False
