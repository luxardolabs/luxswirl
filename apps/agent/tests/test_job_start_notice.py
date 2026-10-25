"""The agent tells the server it started a job before it runs it.

The server marks the job running, so a job the agent picked up is told apart
from one it never got. The notice is best effort: if it fails, the job still
runs and its result is still sent.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from app.agent.job_processor import JobProcessor
from app.shared.jobs.base import BaseJob

pytestmark = pytest.mark.unit

_JOB_ID = "11111111-1111-1111-1111-111111111111"


class _Recorded(BaseJob):
    job_type = "recorded"
    calls: list[str] = []

    async def execute(self) -> dict[str, Any]:
        _Recorded.calls.append("run")
        return {"hosts": 1}


def _processor(handler) -> tuple[JobProcessor, list[str]]:
    seen: list[str] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        _Recorded.calls.append(request.url.path.rsplit("/", 1)[-1])
        return handler(request)

    processor = JobProcessor({"push_url": "http://server.test/api/v1/reports"})
    processor.register_job_type(_Recorded)
    processor.client = httpx.AsyncClient(transport=httpx.MockTransport(recording))
    return processor, seen


async def test_the_start_notice_goes_before_the_job_runs() -> None:
    _Recorded.calls = []
    processor, seen = _processor(lambda r: httpx.Response(200, json={}))

    await processor._execute_job({"job_id": _JOB_ID, "job_type": "recorded"})

    assert seen == [
        f"POST /api/v1/jobs/{_JOB_ID}/start",
        f"POST /api/v1/jobs/{_JOB_ID}/results",
    ]
    assert _Recorded.calls == ["start", "run", "results"]


@pytest.mark.parametrize("answer", ["refused", "unreachable"])
async def test_a_failed_start_notice_still_runs_the_job(answer: str) -> None:
    _Recorded.calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/start"):
            if answer == "unreachable":
                raise httpx.ConnectError("down", request=request)
            return httpx.Response(500)
        return httpx.Response(200, json={})

    processor, seen = _processor(handler)

    await processor._execute_job({"job_id": _JOB_ID, "job_type": "recorded"})

    assert "run" in _Recorded.calls
    assert seen[-1] == f"POST /api/v1/jobs/{_JOB_ID}/results"
    assert processor.stats["total_completed"] == 1


async def test_an_unknown_job_type_sends_no_start_notice() -> None:
    _Recorded.calls = []
    processor, seen = _processor(lambda r: httpx.Response(200, json={}))

    await processor._execute_job({"job_id": _JOB_ID, "job_type": "nope"})

    assert seen == [f"POST /api/v1/jobs/{_JOB_ID}/results"]
