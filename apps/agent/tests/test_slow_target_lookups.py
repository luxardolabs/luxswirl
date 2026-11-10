"""A check target whose DNS is slow never stalls the agent's own work.

A check's name lookup blocks its thread for as long as the resolver takes. Run on
the event loop it stops every task, the heartbeat and the reporter with it; run on
the loop's default executor it queues the agent's connection to its server behind
the slow site. Checks look names up on a pool of their own (`checks._lookups`), each
lookup bounded, and a lookup already running for a host is shared.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections import defaultdict
from typing import Any
from uuid import uuid4

import pytest
from app.agent.check_executor import CheckExecutor
from app.checks import _lookups
from app.checks.base import BaseCheck
from app.checks.dns import DNSCheck
from app.checks.ping import PingCheck
from app.checks.tcp import TCPCheck

pytestmark = pytest.mark.unit

SLOW = "slow.test"
SLOW_SECONDS = 1.0


@pytest.fixture
def slow_dns(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """`slow.test` takes SLOW_SECONDS to fail to resolve; other names resolve as usual.
    Returns the names each slow lookup was asked for."""
    real = socket.getaddrinfo
    asked: list[str] = []
    lock = threading.Lock()

    def fake(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        name = host.decode() if isinstance(host, bytes) else host
        if name == SLOW:
            with lock:
                asked.append(name)
            time.sleep(SLOW_SECONDS)
            raise socket.gaierror(
                socket.EAI_AGAIN, "Temporary failure in name resolution"
            )
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return asked


def _cfg(check_type: str, **overrides: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "check_id": str(uuid4()),
        "name": f"{check_type}-slow",
        "check_type": check_type,
        "target": SLOW,
        "retries": 1,
        "timeout": 5,
    }
    cfg.update(overrides)
    return cfg


def _slow_check(check_type: str) -> BaseCheck:
    if check_type == "ping":
        return PingCheck(_cfg("ping"))
    if check_type == "tcp":
        return TCPCheck(_cfg("tcp", port=443))
    return DNSCheck(_cfg("dns", target="example.com", record_type="A", nameserver=SLOW))


async def _longest_stall(work: Any) -> float:
    """The longest the event loop went without running a 20 ms ticker while `work` ran."""
    gaps: list[float] = []
    done = asyncio.Event()
    ticking = asyncio.Event()

    async def ticker() -> None:
        last = time.monotonic()
        ticking.set()
        while not done.is_set():
            await asyncio.sleep(0.02)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    await ticking.wait()
    try:
        await work
    finally:
        done.set()
        await task
    return max(gaps, default=0.0)


@pytest.mark.parametrize("check_type", ["ping", "tcp", "dns"])
async def test_a_slow_lookup_leaves_the_event_loop_running(
    slow_dns: list[str], check_type: str
) -> None:
    result = await asyncio.wait_for(_slow_check(check_type).run(), 10)
    assert slow_dns, "the check never looked the slow name up"
    stall = await _longest_stall(_slow_check(check_type).run())
    assert stall < 0.5, f"the event loop stalled {stall:.2f} s on a slow lookup"
    assert result["success"] is False


async def test_slow_lookups_leave_the_default_executor_free(
    slow_dns: list[str],
) -> None:
    """The agent's own server traffic resolves on the loop's default executor: more
    slow targets at once than it has threads never queue it."""
    checks = [TCPCheck(_cfg("tcp", port=443)) for _ in range(80)]
    running = [asyncio.create_task(c.run()) for c in checks]
    await asyncio.sleep(0.1)
    assert slow_dns, "no check had started its lookup"
    loop = asyncio.get_running_loop()
    begun = time.monotonic()
    await loop.getaddrinfo("localhost", 9000, type=socket.SOCK_STREAM)
    server_lookup = time.monotonic() - begun
    await asyncio.gather(*running)
    assert server_lookup < 0.5, (
        f"the server's lookup waited {server_lookup:.2f} s behind check lookups"
    )


async def test_a_lookup_running_for_a_host_is_shared(slow_dns: list[str]) -> None:
    results = await asyncio.gather(*(PingCheck(_cfg("ping")).run() for _ in range(20)))
    assert len(slow_dns) == 1
    assert all(r["error"] == f"Could not resolve host: {SLOW}" for r in results)


async def test_a_lookup_is_bounded(
    slow_dns: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_lookups, "LOOKUP_SECONDS", 0.2)
    begun = time.monotonic()
    result = await PingCheck(_cfg("ping")).run()
    assert time.monotonic() - begun < SLOW_SECONDS
    assert result["success"] is False
    assert (
        result["error"]
        == f"Could not resolve host: {SLOW} (the name lookup took over 0.2 s)"
    )


async def test_a_timed_out_lookup_is_the_checks_failed_result(
    slow_dns: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_lookups, "LOOKUP_SECONDS", 0.2)
    executor = CheckExecutor(
        config={"agent_id": "a1"},
        metrics=defaultdict(int),
        check_stats=defaultdict(dict),
        semaphore=asyncio.Semaphore(1),
        result_queue=asyncio.Queue(),
        last_state={},
        run_id="r1",
    )
    result, _ = await executor._run(_slow_check("dns"), timeout=5, longest=0)
    assert result["success"] is False
    assert result["error"] == "the name lookup took over 0.2 s"
