"""The agent's job queue orders jobs without comparing them, and reports what is running.

1. Queue entries were (priority, time.time(), job_data). Two jobs with the same
   priority and the same clock reading made the heap compare the job dicts, which
   raises TypeError, so enqueuing a heartbeat's jobs failed. Equal priorities now
   tie-break on arrival order and never reach the dicts.
2. `jobs_running` in the heartbeat read `active_jobs`, which nothing ever filled:
   the server was always told 0 jobs were running.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from app.agent.job_processor import JobProcessor

pytestmark = pytest.mark.unit


async def test_jobs_with_equal_priority_enqueue_in_arrival_order() -> None:
    processor = JobProcessor({})

    await processor.enqueue_jobs(
        [
            {"job_id": "a", "priority": 5, "job_type": "x"},
            {"job_id": "b", "priority": 5, "job_type": "x"},
        ]
    )

    assert processor.job_queue.qsize() == 2
    first = processor.job_queue.get_nowait()[-1]
    second = processor.job_queue.get_nowait()[-1]
    assert [first["job_id"], second["job_id"]] == ["a", "b"]


async def test_higher_priority_runs_first() -> None:
    processor = JobProcessor({})

    await processor.enqueue_jobs(
        [{"job_id": "low", "priority": 1}, {"job_id": "high", "priority": 9}]
    )

    assert processor.job_queue.get_nowait()[-1]["job_id"] == "high"


async def test_a_running_job_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    processor = JobProcessor({})
    started = asyncio.Event()
    release = asyncio.Event()
    seen_while_running: list[int] = []

    async def _slow_job(job_data: dict[str, Any]) -> None:
        started.set()
        seen_while_running.append(processor.get_stats()["jobs_running"])
        await release.wait()

    monkeypatch.setattr(processor, "_execute_job", _slow_job)
    processor.running = True
    worker = asyncio.create_task(processor._job_worker())
    await processor.enqueue_jobs([{"job_id": "j1", "priority": 0}])

    await asyncio.wait_for(started.wait(), timeout=2)
    release.set()
    await asyncio.sleep(0.05)
    processor.running = False
    await asyncio.wait_for(worker, timeout=3)

    assert seen_while_running == [1]
    assert processor.get_stats()["jobs_running"] == 0
