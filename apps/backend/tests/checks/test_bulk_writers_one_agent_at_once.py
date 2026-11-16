"""Two bulk creates, or two imports, for one agent at once name each check once,
and the hold that serialises them leaves the agent's own requests free.

Each run reads the agent's check names and then makes its own. The runs are
held until both could have read the names (or a run has waited past the
hold), so a run that reads before the other's checks commit would make a
second check of a name already taken.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import pytest
from app.crud.agent_crud import AgentCRUD
from app.crud.check_crud import CheckCRUD
from app.models.agent_model import Agent
from app.models.check_model import Check
from app.schemas.check_schema import BulkCheckCreateRequest
from app.services.core.check_core_service import CheckCoreService
from fixtures.factories import make_agent
from sqlalchemy import select, update

pytestmark = pytest.mark.integration

COUNT = 10
# How long a run that has read the names waits for the other to read them too.
# A run the agent's lock holds back never reads until the first commits.
HOLD_SECONDS = 2


def _hold_after(original: Callable[..., Awaitable[Any]]) -> Callable[..., Any]:
    """`original`, then a wait until a second caller has also returned from it."""
    arrived = 0
    both = asyncio.Event()

    async def read_then_wait(*args: Any, **kwargs: Any) -> Any:
        nonlocal arrived
        found = await original(*args, **kwargs)
        arrived += 1
        if arrived >= 2:
            both.set()
        try:
            await asyncio.wait_for(both.wait(), HOLD_SECONDS)
        except TimeoutError:
            pass
        return found

    return read_then_wait


async def _agent(worker_db) -> UUID:
    async with worker_db() as s:
        agent = make_agent()
        s.add(agent)
        await s.commit()
        return agent.id


async def _names(worker_db, agent_id: UUID) -> list[str]:
    async with worker_db() as s:
        return list(
            (
                await s.scalars(
                    select(Check.display_name).where(Check.agent_id == agent_id)
                )
            ).all()
        )


async def test_two_bulk_creates_at_once_number_the_second_runs_names(
    worker_db, monkeypatch
):
    agent_id = await _agent(worker_db)
    monkeypatch.setattr(
        CheckCRUD,
        "display_names_for_agent",
        staticmethod(_hold_after(CheckCRUD.display_names_for_agent)),
    )
    requests = [
        BulkCheckCreateRequest(url=f"https://127.0.0.1:{8000 + i}/")
        for i in range(COUNT)
    ]

    async def create() -> int:
        async with worker_db() as s:
            result = await CheckCoreService.bulk_create_checks(s, agent_id, requests)
            await s.commit()
            return result.succeeded

    assert await asyncio.gather(create(), create()) == [COUNT, COUNT]

    names = await _names(worker_db, agent_id)
    assert len(names) == 2 * COUNT
    assert len(set(names)) == len(names), (
        f"{len(names) - len(set(names))} names made twice"
    )


async def test_two_imports_at_once_make_each_check_once(worker_db, monkeypatch):
    agent_id = await _agent(worker_db)
    monkeypatch.setattr(
        CheckCoreService,
        "list_checks_for_agent",
        staticmethod(_hold_after(CheckCoreService.list_checks_for_agent)),
    )
    entries = [
        {
            "name": f"imported-{i}",
            "check_type": "tcp",
            "target": f"127.0.0.1:{8000 + i}",
        }
        for i in range(COUNT)
    ]

    async def import_() -> tuple[int, int]:
        async with worker_db() as s:
            result = await CheckCoreService.bulk_import_checks(
                s, agent_id, entries, overwrite=False, actor_is_admin=True
            )
            await s.commit()
            assert result["errors"] == []
            return result["created"], result["skipped"]

    assert sorted(await asyncio.gather(import_(), import_())) == [
        (0, COUNT),
        (COUNT, 0),
    ]

    names = await _names(worker_db, agent_id)
    assert sorted(names) == sorted(e["name"] for e in entries)


async def test_the_names_hold_waits_for_another_writer_not_for_the_agent(
    worker_db,
):
    """An import can run for minutes: the agent's reports (FOR KEY SHARE on its
    row) and heartbeat (an update of its row) go on meanwhile."""
    agent_id = await _agent(worker_db)
    async with worker_db() as holder:
        await CheckCRUD.hold_names(holder, agent_id)

        async with worker_db() as other:
            admission = await asyncio.wait_for(
                AgentCRUD.key_share_admission(other, agent_id), HOLD_SECONDS
            )
            assert admission is not None
            await asyncio.wait_for(
                other.execute(
                    update(Agent)
                    .where(Agent.id == agent_id)
                    .values(hostname="heartbeat")
                ),
                HOLD_SECONDS,
            )
            await other.commit()

        async with worker_db() as writer:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(
                    CheckCRUD.hold_names(writer, agent_id), HOLD_SECONDS
                )
            await writer.rollback()
        await holder.rollback()
