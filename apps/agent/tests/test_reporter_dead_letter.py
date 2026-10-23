"""A batch the server permanently refuses is dead-lettered, never retried forever.

A 400/413/422 means the server refuses this payload and will again, so the
batch goes to the `dead_letters` table instead of the pending queue, and replay
of the pending queue never stops at it. Everything else (network errors, 5xx,
408, 429, and 401/403, which key recovery can fix) stays transient: the batch
is stored and retried as before.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.agent import http_client
from app.agent.dead_letter import Delivery, classify_status
from app.agent.heartbeat import HeartbeatSender
from app.agent.reporter import Reporter

pytestmark = pytest.mark.unit

Handler = Callable[[httpx.Request], httpx.Response]


async def _reporter(tmp_path: Path, handler: Handler, **overrides: Any) -> Reporter:
    config: dict[str, Any] = {
        "agent_id": "a1",
        "push_url": "https://server.example/api/v1/reports",
        "auth_key": "k1",
        "report_storage_path": str(tmp_path),
        "report_max_retries": 2,
        "report_retry_delay": 0,
        "report_process_interval": 1,
        "report_batch_size": 5000,
        **overrides,
    }
    reporter = Reporter(config)
    reporter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reporter.running = True
    await reporter._init_storage()
    return reporter


def _rows(tmp_path: Path, sql: str) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(str(tmp_path / "pending_reports.db"))
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _pending(tmp_path: Path) -> int:
    return int(_rows(tmp_path, "SELECT COUNT(*) FROM pending_batches")[0][0])


def _dead_payloads(tmp_path: Path) -> list[list[dict[str, Any]]]:
    return [
        json.loads(gzip.decompress(payload))
        for (payload,) in _rows(
            tmp_path, "SELECT payload FROM dead_letters ORDER BY id"
        )
    ]


def _result(name: str) -> dict[str, Any]:
    return {"check_id": name, "timestamp": 1_760_000_000.0, "success": True}


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (200, Delivery.SENT),
        (201, Delivery.SENT),
        (204, Delivery.SENT),
        (400, Delivery.PERMANENT),
        (413, Delivery.PERMANENT),
        (422, Delivery.PERMANENT),
        (401, Delivery.TRANSIENT),
        (403, Delivery.TRANSIENT),
        (404, Delivery.TRANSIENT),
        (408, Delivery.TRANSIENT),
        (429, Delivery.TRANSIENT),
        (500, Delivery.TRANSIENT),
        (502, Delivery.TRANSIENT),
        (503, Delivery.TRANSIENT),
        (504, Delivery.TRANSIENT),
        (301, Delivery.TRANSIENT),
    ],
)
def test_classify_status(status: int, expected: Delivery) -> None:
    assert classify_status(status) is expected


async def test_a_422_is_dead_lettered_once_and_not_stored(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(422, json={"detail": "status 'weird' is not allowed"})

    reporter = await _reporter(tmp_path, handler)

    assert await reporter.send_batch([_result("c1"), _result("c2")]) is False

    assert len(calls) == 1  # a permanent refusal is not retried
    assert _pending(tmp_path) == 0
    assert _dead_payloads(tmp_path) == [[_result("c1"), _result("c2")]]
    [(status, detail)] = _rows(
        tmp_path, "SELECT status_code, response_body FROM dead_letters"
    )
    assert status == 422
    assert "is not allowed" in detail
    assert reporter.get_metrics()["dead_letter_count"] == 1


@pytest.mark.parametrize("status", [503, 401, 403, 429])
async def test_a_transient_failure_is_stored_never_dead_lettered(
    tmp_path: Path, status: int
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json={"detail": "try later"})

    reporter = await _reporter(tmp_path, handler)

    assert await reporter.send_batch([_result("c1")]) is False

    assert len(calls) == 2  # retried up to report_max_retries
    assert _pending(tmp_path) == 1
    assert _dead_payloads(tmp_path) == []


async def test_a_network_error_is_stored(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    reporter = await _reporter(tmp_path, handler)

    assert await reporter.send_batch([_result("c1")]) is False

    assert _pending(tmp_path) == 1
    assert _dead_payloads(tmp_path) == []


async def test_the_stored_detail_is_scrubbed_and_truncated(tmp_path: Path) -> None:
    body = "Bearer sekrit-token " + "x" * 10_000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, text=body)

    reporter = await _reporter(tmp_path, handler)
    await reporter.send_batch([_result("c1")])

    [(detail,)] = _rows(tmp_path, "SELECT response_body FROM dead_letters")
    assert "sekrit-token" not in detail
    assert detail.startswith("Bearer ***")
    assert len(detail) <= 2048


async def test_the_cap_prunes_the_oldest_dead_letters(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "no"})

    reporter = await _reporter(tmp_path, handler, report_max_dead_letters=2)
    for name in ("first", "second", "third"):
        await reporter.send_batch([_result(name)])

    assert _dead_payloads(tmp_path) == [[_result("second")], [_result("third")]]


async def test_replay_dead_letters_the_refused_batch_and_drains_the_rest(
    tmp_path: Path,
) -> None:
    """A batch the server refuses for good never blocks the batches behind it:
    a combined replay POST that comes back 422 is retried batch by batch."""
    received: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        checks = json.loads(request.content)["checks"]
        if any(c["check_id"] == "bad" for c in checks):
            return httpx.Response(422, json={"detail": "bad value"})
        received.extend(c["check_id"] for c in checks)
        return httpx.Response(200, json={})

    reporter = await _reporter(tmp_path, handler)
    for name in ("good-1", "bad", "good-2", "good-3"):
        reporter._store_batch_to_db_sync([_result(name)])
        await asyncio.sleep(0.01)  # distinct created_at, so replay order is defined

    task = asyncio.create_task(reporter.process_stored_reports())
    try:
        deadline = time.monotonic() + 5
        while _pending(tmp_path) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
    finally:
        reporter.running = False
        await asyncio.wait_for(task, timeout=5)

    assert sorted(received) == ["good-1", "good-2", "good-3"]
    assert _pending(tmp_path) == 0
    assert _dead_payloads(tmp_path) == [[_result("bad")]]


async def test_replay_stops_at_a_transient_failure_and_keeps_the_queue(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "down"})

    reporter = await _reporter(tmp_path, handler)
    for name in ("one", "two"):
        reporter._store_batch_to_db_sync([_result(name)])

    task = asyncio.create_task(reporter.process_stored_reports())
    await asyncio.sleep(0.5)
    reporter.running = False
    await asyncio.wait_for(task, timeout=5)

    assert _pending(tmp_path) == 2  # nothing lost, nothing duplicated
    assert _dead_payloads(tmp_path) == []


class _StubReporter:
    def get_metrics(self) -> dict[str, Any]:
        return {"stored_reports_count": 0, "dead_letter_count": 3}


class _StubJobs:
    def get_stats(self) -> dict[str, int]:
        return {}


class _StubMemory:
    rss = 64 * 1024 * 1024


class _StubProcess:
    def cpu_percent(self, interval: float) -> float:
        return 1.0

    def memory_info(self) -> _StubMemory:
        return _StubMemory()


async def test_the_heartbeat_carries_dead_letter_count() -> None:
    bodies: list[dict[str, Any]] = []
    sender: HeartbeatSender

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        sender.stop()
        return httpx.Response(200, json={})

    http_client._server_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    sender = HeartbeatSender(
        config={
            "agent_id": "a1",
            "push_url": "https://server.example/api/v1/reports",
            "auth_key": "k1",
            "resource_monitoring_enabled": False,
        },
        metrics={
            "start_time": time.time(),
            "checks_executed": 0,
            "checks_succeeded": 0,
            "checks_failed": 0,
            "result_queue_max_size": 0,
            "heartbeats": 0,
        },
        reporter=_StubReporter(),
        job_processor=_StubJobs(),
        credentials=None,
        process=_StubProcess(),
        on_config_reload=None,
        on_agent_approved=None,
    )
    try:
        await asyncio.wait_for(sender.send_heartbeat(), timeout=10)
    finally:
        await http_client.aclose_all()

    assert bodies[0]["dead_letter_count"] == 3
