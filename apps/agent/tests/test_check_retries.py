"""A failing check is retried, and the run is reported once.

- Up to `retry_attempts` more tries after the first, `retry_interval` seconds apart.
- A check that succeeds on a retry reports one success; the attempt count is in
  its metrics (`metrics.attempts`).
- `timeout` applies to each attempt.
- No retry starts unless it can finish before the check's next scheduled run
  (`interval`), so a retrying run never overlaps the next one.
- The wait between attempts holds no concurrency slot.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Any

import pytest
from app.agent.check_executor import CheckExecutor
from app.checks.base import BaseCheck

pytestmark = pytest.mark.unit


class _Scripted(BaseCheck):
    """Each run() takes the next outcome: True/False, or a float to sleep (a hang)."""

    def __init__(
        self, config: dict[str, Any], outcomes: list[Any], cleanup: float = 0.0
    ) -> None:
        super().__init__(config)
        self.outcomes = list(outcomes)
        self.cleanup = cleanup
        self.started: list[float] = []
        self.ended: list[float] = []

    def validate_config(self, config: dict[str, Any]) -> None:
        return None

    async def run(self) -> dict[str, Any]:
        self.started.append(time.monotonic())
        outcome = self.outcomes.pop(0) if self.outcomes else False
        try:
            if isinstance(outcome, float):
                await asyncio.sleep(outcome)
                outcome = True
            return self.create_result(
                success=outcome,
                latency_ms=1.0,
                error=None if outcome else "down",
                metrics={"http": {"status": 200 if outcome else 503}},
            )
        finally:
            if self.cleanup:
                # e.g. a browser closing after the attempt is cancelled
                await asyncio.shield(asyncio.sleep(self.cleanup))
            self.ended.append(time.monotonic())


def _check(outcomes: list[Any], cleanup: float = 0.0, **cfg: Any) -> _Scripted:
    config = {
        "check_id": "00000000-0000-0000-0000-000000000001",
        "name": "c1",
        "check_type": "http",
        "target": "https://example.test",
        "interval": 60,
        "timeout": 1,
        "retry_attempts": 0,
        "retry_interval": 0.05,
        **cfg,
    }
    return _Scripted(config, outcomes, cleanup)


def _executor(slots: int = 5) -> tuple[CheckExecutor, asyncio.Queue]:
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    stats: defaultdict[str, dict[str, float]] = defaultdict(
        lambda: {"total_runs": 0, "successes": 0, "failures": 0, "total_latency": 0}
    )
    metrics = defaultdict(int)
    executor = CheckExecutor(
        config={"agent_id": "a1"},
        metrics=metrics,
        check_stats=stats,
        semaphore=asyncio.Semaphore(slots),
        result_queue=queue,
        last_state={},
        run_id="r1",
    )
    return executor, queue


def _drain(queue: asyncio.Queue) -> list[dict[str, Any]]:
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


async def test_a_check_that_recovers_on_a_retry_reports_one_success() -> None:
    executor, queue = _executor()
    check = _check([False, True], retry_attempts=2)

    await executor.run_check(check)

    results = _drain(queue)
    assert [r["success"] for r in results] == [True]
    assert results[0]["metrics"]["attempts"] == 2
    assert results[0]["metrics"]["http"] == {"status": 200}
    assert executor.metrics["checks_failed"] == 0


async def test_retries_wait_the_retry_interval_between_attempts() -> None:
    executor, _ = _executor()
    check = _check([False, False, True], retry_attempts=2, retry_interval=0.2)

    await executor.run_check(check)

    gaps = [b - a for a, b in zip(check.started, check.started[1:], strict=False)]
    assert len(gaps) == 2
    assert all(gap >= 0.2 for gap in gaps), gaps


async def test_a_check_that_keeps_failing_reports_one_failure_after_every_attempt() -> (
    None
):
    executor, queue = _executor()
    check = _check([False, False, False, True], retry_attempts=2)

    await executor.run_check(check)

    results = _drain(queue)
    assert [r["success"] for r in results] == [False]
    assert results[0]["metrics"]["attempts"] == 3
    assert len(check.started) == 3
    assert executor.metrics["checks_failed"] == 1


async def test_no_retry_attempts_means_one_try() -> None:
    executor, queue = _executor()
    check = _check([False, True], retry_attempts=None)

    await executor.run_check(check)

    assert len(check.started) == 1
    assert _drain(queue)[0]["metrics"]["attempts"] == 1


async def test_the_timeout_applies_to_each_attempt() -> None:
    """A first attempt that hangs past the timeout is cut off; the retry gets
    its own full timeout and succeeds."""
    executor, queue = _executor()
    check = _check([5.0, True], retry_attempts=1, timeout=0.3)

    await executor.run_check(check)

    results = _drain(queue)
    assert [r["success"] for r in results] == [True]
    assert results[0]["metrics"]["attempts"] == 2


async def test_a_run_that_times_out_every_attempt_reports_the_timeout() -> None:
    executor, queue = _executor()
    check = _check([5.0, 5.0], retry_attempts=1, timeout=0.2)

    await executor.run_check(check)

    results = _drain(queue)
    assert [(r["success"], r["error"]) for r in results] == [(False, "timeout")]
    assert results[0]["metrics"]["attempts"] == 2


async def test_no_retry_starts_that_could_overrun_the_next_scheduled_run() -> None:
    """interval 1s, timeout 0.2s, retry interval 0.5s: the second attempt can
    finish by 0.7s, a third could not finish before 1.2s, so it never starts."""
    executor, queue = _executor()
    check = _check(
        [False] * 6, retry_attempts=5, interval=1, timeout=0.2, retry_interval=0.5
    )

    started = time.monotonic()
    await executor.run_check(check)

    assert len(check.started) == 2
    assert time.monotonic() - started < 1
    assert _drain(queue)[0]["metrics"]["attempts"] == 2


async def test_the_wait_between_attempts_holds_no_concurrency_slot() -> None:
    executor, queue = _executor(slots=1)
    retrying = _check([False, True], retry_attempts=1, retry_interval=0.5)
    other = _check([True], name="c2")

    task = asyncio.create_task(executor.run_check(retrying))
    await asyncio.sleep(0.1)  # the first attempt has failed; its wait has begun
    await asyncio.wait_for(executor.run_check(other), timeout=0.3)
    await task

    names = [r["display_name"] for r in _drain(queue)]
    assert names == ["c2", "c1"]


async def test_a_retry_that_waited_for_a_slot_too_long_is_not_started() -> None:
    """interval 1s, timeout 0.2s: the retry is due at 0.1s, but the only slot is
    busy until ~0.95s, too late for an attempt to finish before 1s."""
    executor, queue = _executor(slots=1)
    check = _check(
        [False, True], retry_attempts=1, interval=1.0, timeout=0.2, retry_interval=0.1
    )
    hog = _check([0.95], name="hog", timeout=5)

    started = time.monotonic()
    task = asyncio.create_task(executor.run_check(check))
    await asyncio.sleep(0.02)  # the first attempt has failed; its wait has begun
    await asyncio.gather(task, executor.run_check(hog))

    assert len(check.started) == 1
    assert check.ended[-1] - started < 1.0
    result = next(r for r in _drain(queue) if r["display_name"] == "c1")
    assert (result["success"], result["metrics"]["attempts"]) == (False, 1)


async def test_an_attempt_that_outlasts_its_timeout_counts_its_real_length() -> None:
    """A timed-out attempt ends when its cleanup does (0.2s + 0.4s here), so a
    retry is budgeted for that length, and one that could not finish by the next
    run is not started."""
    executor, queue = _executor()
    check = _check(
        [5.0, 5.0],
        cleanup=0.4,
        retry_attempts=1,
        interval=1.0,
        timeout=0.2,
        retry_interval=0.1,
    )

    started = time.monotonic()
    await executor.run_check(check)

    assert len(check.started) == 1
    assert check.ended[-1] - started < 1.0
    assert _drain(queue)[0]["error"] == "timeout"


async def test_a_check_with_no_timeout_still_reports_its_run() -> None:
    executor, queue = _executor()
    check = _check([True], timeout=None)

    await executor.run_check(check)

    assert [r["success"] for r in _drain(queue)] == [True]


async def test_a_retried_run_is_stamped_with_the_time_it_started() -> None:
    """Results arrive one interval apart as the server expects, however long a
    run spent retrying."""
    executor, queue = _executor()
    check = _check([False, True], retry_attempts=1, retry_interval=0.3)

    before = time.time()
    await executor.run_check(check)

    assert _drain(queue)[0]["timestamp"] - before < 0.1
