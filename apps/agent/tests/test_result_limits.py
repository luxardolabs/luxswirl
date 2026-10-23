"""A result is fitted to the server's per-result limits before it is queued.

The server skips a result outside its schema, so the agent truncates an over-
long error or response body and moves an HTTP status outside 100-599 into the
metrics, where the result is still stored.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.agent.reporter import Reporter
from app.agent.result_limits import (
    MAX_ERROR_CHARS,
    MAX_RESPONSE_DATA_CHARS,
    TRUNCATED_MARKER,
    fit_result,
)

pytestmark = pytest.mark.unit


def _result(**kw: Any) -> dict[str, Any]:
    return {"check_id": "c1", "success": False, "latency_ms": 1.0, **kw}


def test_a_long_error_is_truncated_to_the_server_limit() -> None:
    out = fit_result(_result(error="x" * (MAX_ERROR_CHARS + 1)))

    assert len(out["error"]) == MAX_ERROR_CHARS
    assert out["error"].endswith(TRUNCATED_MARKER)


def test_an_error_at_the_limit_is_untouched() -> None:
    error = "x" * MAX_ERROR_CHARS
    assert fit_result(_result(error=error))["error"] == error


def test_long_response_data_is_truncated() -> None:
    out = fit_result(_result(response_data="y" * (MAX_RESPONSE_DATA_CHARS + 50)))

    assert len(out["response_data"]) == MAX_RESPONSE_DATA_CHARS


@pytest.mark.parametrize("status", [999, 99, 0, 600, -1])
def test_an_out_of_range_status_moves_to_the_metrics(status: int) -> None:
    out = fit_result(_result(http_status_code=status, metrics={"response": {}}))

    assert out["http_status_code"] is None
    assert out["metrics"] == {"response": {}, "http_status_code_raw": status}


def test_an_out_of_range_status_without_metrics_gets_some() -> None:
    out = fit_result(_result(http_status_code=999))

    assert out["metrics"] == {"http_status_code_raw": 999}


@pytest.mark.parametrize("status", [100, 200, 404, 599, None])
def test_an_in_range_status_is_untouched(status: int | None) -> None:
    out = fit_result(_result(http_status_code=status))

    assert out["http_status_code"] == status
    assert "metrics" not in out


async def test_the_reporter_fits_every_result_it_queues(tmp_path) -> None:
    reporter = Reporter(
        {
            "agent_id": "a1",
            "push_url": "https://server.example/api/v1/reports",
            "auth_key": "k1",
            "report_storage_path": str(tmp_path),
        }
    )
    reporter.running = True

    await reporter.add_result(_result(error="e" * 5000, http_status_code=999))

    [queued] = reporter.current_batch
    assert len(queued["error"]) == MAX_ERROR_CHARS
    assert queued["http_status_code"] is None
