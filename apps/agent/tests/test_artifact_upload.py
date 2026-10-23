"""The agent uploads artifacts as itself: its own key and its own id.

The server authenticates POST /api/v1/artifacts like a report, by the agent
named in the body proving its own key, so the upload must carry both.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from app.agent import http_client
from app.agent.reporter import Reporter
from app.agent.result_processor import ResultProcessor

pytestmark = pytest.mark.unit


@pytest_asyncio.fixture
async def seen() -> AsyncIterator[list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={})

    http_client._server_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    try:
        yield requests
    finally:
        await http_client.aclose_all()


def _result() -> dict[str, Any]:
    return {
        "check_id": "c1",
        "result_id": "r1",
        "timestamp": 1_760_000_000.0,
        "artifacts": [
            {
                "type": "screenshot",
                "content_type": "image/png",
                "filename": "shot.png",
                "data": b"png",
                "size_bytes": 3,
            }
        ],
    }


async def test_the_upload_carries_the_agent_id_and_its_own_key(
    seen: list[httpx.Request], tmp_path: Path
) -> None:
    config: dict[str, Any] = {
        "agent_id": "agent-1",
        "push_url": "https://server.example/api/v1/reports",
        "auth_key": "luxswirl_ak_own",
        "report_storage_path": str(tmp_path),
    }
    processor = ResultProcessor(
        config=config, reporter=Reporter(config), result_queue=asyncio.Queue()
    )
    result = _result()

    await processor.upload_artifacts(result)

    assert len(seen) == 1
    body = json.loads(seen[0].content)
    assert str(seen[0].url) == "https://server.example/api/v1/artifacts"
    assert seen[0].headers["Authorization"] == "Bearer luxswirl_ak_own"
    assert body["agent_id"] == "agent-1"
    assert body["check_id"] == "c1"
    assert "artifacts" not in result
