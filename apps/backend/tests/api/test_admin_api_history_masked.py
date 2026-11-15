"""A viewer reads a history bucket's errors each as its own result's is read.

A bucket holds several results, and its metrics are its newest result's alone. A
size-noted run (a script's own error, with no agent prefix) bucketed with a newer
ordinary run is still read as a failure alone, and an error that starts as the
agent writes a synthetic run's is read as one wherever it sorts in the bucket.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.rate_limit import limiter
from app.main import app
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.api_token_core_service import ApiTokenCoreService
from fixtures.factories import make_agent, make_check, make_check_result, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

CHECKS = "/api/v1/admin/checks"
TOKEN = "Zq8resetTOKENpath42"
# What a script reports in its own `errors` (the agent joins them first, no prefix).
SCRIPT_ERROR = f"Reset link https://app.example.com/reset/{TOKEN} did not load"
# The agent's size note (apps/agent/app/agent/result_limits.py).
SIZE_NOTE = json.dumps({"metrics_truncated": True, "metrics_original_chars": 91234})


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    limiter.reset()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as c:
        yield c
    limiter.reset()


async def _viewer(db: AsyncSession) -> dict[str, str]:
    user = make_user(role="viewer")
    db.add(user)
    await db.flush()
    _, plaintext = await ApiTokenCoreService.create_token(
        db, user.id, ApiTokenCreate(name="viewer", scopes=["*"])
    )
    return {"Authorization": f"Bearer {plaintext}"}


async def _retyped_check(
    db: AsyncSession,
    *,
    newer_result: bool,
    first_error: str = SCRIPT_ERROR,
    first_metrics: str = SIZE_NOTE,
):
    """An http check retyped from synthetic before `ran_synthetic` existed (false,
    as the backfill leaves one whose only script runs were size-noted), with a
    size-noted script run and, in the same minute, optionally a newer http run."""
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(agent_id=agent.id, check_type="http")
    check.ran_synthetic = False
    db.add(check)
    await db.flush()
    minute = utc_now().replace(second=0, microsecond=0) - timedelta(minutes=5)
    db.add(
        make_check_result(
            check_id=check.id,
            agent_id=agent.id,
            timestamp=minute + timedelta(seconds=10),
            success=False,
            error=first_error,
            metrics=first_metrics,
        )
    )
    if newer_result:
        db.add(
            make_check_result(
                check_id=check.id,
                agent_id=agent.id,
                timestamp=minute + timedelta(seconds=40),
                success=False,
                error="HTTP 503 Service Unavailable",
                metrics="{}",
            )
        )
    await db.flush()
    return check


async def test_control_results_list_hides_the_size_noted_script_error(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _viewer(db)
    check = await _retyped_check(db, newer_result=True)

    resp = await client.get(f"{CHECKS}/{check.id}/results", headers=viewer)

    assert resp.status_code == 200, resp.text
    assert TOKEN not in resp.text
    assert "Check failed" in resp.text


async def test_control_history_alone_hides_the_size_noted_script_error(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _viewer(db)
    check = await _retyped_check(db, newer_result=False)

    resp = await client.get(
        f"{CHECKS}/{check.id}/history", params={"hours": 1}, headers=viewer
    )

    assert resp.status_code == 200, resp.text
    assert TOKEN not in resp.text


async def test_a_history_bucket_keeps_a_size_noted_runs_error_from_a_viewer(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _viewer(db)
    check = await _retyped_check(db, newer_result=True)

    resp = await client.get(
        f"{CHECKS}/{check.id}/history", params={"hours": 1}, headers=viewer
    )

    assert resp.status_code == 200, resp.text
    assert TOKEN not in resp.text, resp.text


async def test_an_editor_reads_every_error_of_a_history_bucket(
    db: AsyncSession, client: AsyncClient
) -> None:
    user = make_user(role="editor")
    db.add(user)
    await db.flush()
    _, plaintext = await ApiTokenCoreService.create_token(
        db, user.id, ApiTokenCreate(name="editor", scopes=["*"])
    )
    check = await _retyped_check(db, newer_result=True)

    resp = await client.get(
        f"{CHECKS}/{check.id}/history",
        params={"hours": 1},
        headers={"Authorization": f"Bearer {plaintext}"},
    )

    assert resp.status_code == 200, resp.text
    assert TOKEN in resp.text
    assert "HTTP 503 Service Unavailable" in resp.text


async def test_a_synthetic_prefixed_error_sorted_second_in_a_bucket_is_read_as_one(
    db: AsyncSession, client: AsyncClient
) -> None:
    """The bucket's distinct errors sort as text, so the run a script wrote need
    not come first."""
    viewer = await _viewer(db)
    check = await _retyped_check(
        db,
        newer_result=True,
        first_error=f"Script execution failed: Error: {SCRIPT_ERROR}",
        first_metrics="{}",
    )

    resp = await client.get(
        f"{CHECKS}/{check.id}/history", params={"hours": 1}, headers=viewer
    )

    assert resp.status_code == 200, resp.text
    assert TOKEN not in resp.text, resp.text
    assert "Synthetic check failed" in resp.text
