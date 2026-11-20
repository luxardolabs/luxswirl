"""A bulk modify job stamps each agent it changed once, after its last check,
so it does not hold the agent's row for its run.

An editor's bulk modify job (no statement timeout) changes five checks of an
agent in place, each target resolving slowly. An edit of another check on that
agent, made while the job runs, writes its own stamp of the agent's row and
lands while the job runs: the job holds the agent's row only from its one
stamp, after its last check, to its commit.
"""

from __future__ import annotations

import asyncio
import socket
import time
from typing import Any
from uuid import UUID

import pytest
from app.schemas.check_schema import CheckUpdate
from app.services.core.check_core_service import CheckCoreService
from fixtures.factories import make_agent, make_check
from sqlalchemy import text

pytestmark = pytest.mark.integration

SLOW_HOST = "slow.example.test"
TARGET = "https://127.0.0.1/"


def _slow_lookups(monkeypatch, seconds: float) -> None:
    real = socket.getaddrinfo

    def resolve(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host != SLOW_HOST:
            return real(host, *args, **kwargs)
        time.sleep(seconds)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


async def _agent(worker_db) -> UUID:
    async with worker_db() as s:
        agent = make_agent()
        s.add(agent)
        await s.commit()
        return agent.id


async def _check(worker_db, agent_id: UUID, name: str) -> UUID:
    async with worker_db() as s:
        check = make_check(
            agent_id=agent_id, display_name=name, check_type="http", target=TARGET
        )
        s.add(check)
        await s.commit()
        return check.id


async def _bulk_modify_job(worker_db, ids: list[UUID]) -> dict[str, Any]:
    async with worker_db() as s:
        await s.execute(text("SET LOCAL statement_timeout = 0"))
        out = await CheckCoreService.bulk_modify(
            s,
            ids,
            CheckUpdate(target=f"https://{SLOW_HOST}/"),
            actor_is_admin=False,
        )
        await s.commit()
        return out


async def _edit(worker_db, check_id: UUID) -> tuple[str, float]:
    started = time.monotonic()
    async with worker_db() as s:
        try:
            await CheckCoreService.update_check(
                s, check_id, CheckUpdate(interval_seconds=600), actor_is_admin=False
            )
            await s.commit()
            return "done", time.monotonic() - started
        except Exception as e:  # the outcome is what is asserted
            await s.rollback()
            orig = getattr(e, "orig", None)
            return (
                f"{type(e).__name__}:{type(orig).__name__}:{str(orig)[:80]}",
                time.monotonic() - started,
            )


async def test_an_edit_without_a_job_lands(worker_db):
    agent = await _agent(worker_db)
    other = await _check(worker_db, agent, "other")
    outcome, waited = await _edit(worker_db, other)
    assert outcome == "done" and waited < 2, (outcome, waited)


async def test_an_edit_beside_a_bulk_modify_job_lands(worker_db, monkeypatch):
    _slow_lookups(monkeypatch, 2.5)
    agent = await _agent(worker_db)
    ids = [await _check(worker_db, agent, f"site-{i}") for i in range(5)]
    other = await _check(worker_db, agent, "other")
    job = asyncio.create_task(_bulk_modify_job(worker_db, ids))
    await asyncio.sleep(3.5)
    outcome, waited = await _edit(worker_db, other)
    out = await asyncio.wait_for(job, 60)
    assert outcome == "done", (outcome, round(waited, 1))
    assert out["success_count"] == 5, out
