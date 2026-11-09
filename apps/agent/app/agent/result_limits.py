"""Fit a check result to the server's per-result limits before it is queued.

The server validates each result of a report on its own and skips one it
cannot store, so a value outside its schema loses that result. Where the fix is
harmless the agent makes it here instead, once, for every result:

- `error` longer than the server's 4,000 characters is truncated, ending in a
  marker that says so;
- `response_data` longer than 65,535 characters is truncated the same way;
- `http_status_code` outside 100-599 (a real server can answer 999) is sent as
  None, and the code itself goes into `metrics["http_status_code_raw"]`, so the
  result is stored and the odd code is still visible;
- `metrics` larger than 65,535 characters as compact JSON have every string in
  them cut to 4,000 characters (a synthetic check's traceback, a console log);
  metrics still too large (thousands of DNS records) are replaced by a note
  saying how large they were, keeping `http_status_code_raw`.
"""

from __future__ import annotations

import json
from typing import Any

# The server's CheckResultCreate limits.
MAX_ERROR_CHARS = 4000
MAX_RESPONSE_DATA_CHARS = 65535
MAX_METRICS_CHARS = 65535
HTTP_STATUS_MIN = 100
HTTP_STATUS_MAX = 599

# How long any one string inside over-large metrics is kept.
MAX_METRIC_STRING_CHARS = 4000

TRUNCATED_MARKER = "... [truncated]"


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(TRUNCATED_MARKER)] + TRUNCATED_MARKER


def _metrics_size(metrics: object) -> int:
    """The size as the server measures it: compact JSON."""
    return len(
        json.dumps(metrics, separators=(",", ":"), ensure_ascii=False, default=str)
    )


def _truncate_strings(value: object) -> object:
    if isinstance(value, str):
        return _truncate(value, MAX_METRIC_STRING_CHARS)
    if isinstance(value, dict):
        return {key: _truncate_strings(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_truncate_strings(item) for item in value]
    return value


def _fit_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    """`metrics` within MAX_METRICS_CHARS: as they are, strings cut, or a note."""
    original_size = _metrics_size(metrics)
    if original_size <= MAX_METRICS_CHARS:
        return metrics
    trimmed = {key: _truncate_strings(item) for key, item in metrics.items()}
    if _metrics_size(trimmed) <= MAX_METRICS_CHARS:
        return trimmed
    note: dict[str, Any] = {
        "metrics_truncated": True,
        "metrics_original_chars": original_size,
    }
    if "http_status_code_raw" in metrics:
        note["http_status_code_raw"] = metrics["http_status_code_raw"]
    return note


def fit_result(result: dict[str, Any]) -> dict[str, Any]:
    """`result` with its fields fitted to the server's limits (see the module).

    Returns the same dict, changed in place, for chaining.
    """
    error = result.get("error")
    if isinstance(error, str):
        result["error"] = _truncate(error, MAX_ERROR_CHARS)

    response_data = result.get("response_data")
    if isinstance(response_data, str):
        result["response_data"] = _truncate(response_data, MAX_RESPONSE_DATA_CHARS)

    status = result.get("http_status_code")
    if isinstance(status, int) and not HTTP_STATUS_MIN <= status <= HTTP_STATUS_MAX:
        result["http_status_code"] = None
        metrics = result.get("metrics")
        if not isinstance(metrics, dict):
            metrics = {}
            result["metrics"] = metrics
        metrics["http_status_code_raw"] = status

    metrics = result.get("metrics")
    if isinstance(metrics, dict):
        result["metrics"] = _fit_metrics(metrics)

    return result
