"""The writers that choose an agent's check names (bulk create, import, checks
made from a scan) hold the names one transaction at a time, judge the agent
under that hold, and leave the agent's own requests free while they write.

Two writers for one agent at once name each check once. An archive in flight
when a writer starts is waited for and refuses it. During the write, the
agent's reports (FOR KEY SHARE on its row) and heartbeat (an update of its row)
are answered: the agent's checks are stamped as changed once, after the last.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from app.core.exceptions import ValidationException
from app.crud.agent_crud import AgentCRUD
from app.crud.check_crud import CheckCRUD
from app.models.agent_model import Agent
from app.models.check_model import Check
from app.schemas.check_schema import BulkCheckCreateRequest
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.check_core_service import CheckCoreService
from app.services.core.job_to_check_core_service import (
    BulkCheckParams,
    JobToCheckCoreService,
    QuickCheckParams,
)
from fixtures.factories import make_agent
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

COUNT = 10
# How long a run that has read the names waits for the other to read them too,
# and how long a request the write must not block is given.
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
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(both.wait(), HOLD_SECONDS)
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


def _bulk_requests() -> list[BulkCheckCreateRequest]:
    return [
        BulkCheckCreateRequest(url=f"https://127.0.0.1:{8000 + i}/")
        for i in range(COUNT)
    ]


def _import_entries() -> list[dict[str, Any]]:
    return [
        {
            "name": f"imported-{i}",
            "check_type": "tcp",
            "target": f"127.0.0.1:{8000 + i}",
        }
        for i in range(COUNT)
    ]


def _scan_params(agent_id: UUID) -> BulkCheckParams:
    return BulkCheckParams(
        interval=60, timeout=10, retry_attempts=0, agent_id=agent_id, tags=[]
    )


async def _bulk_create(s: AsyncSession, agent_id: UUID) -> None:
    await CheckCoreService.bulk_create_checks(s, agent_id, _bulk_requests())


async def _import(s: AsyncSession, agent_id: UUID) -> None:
    await CheckCoreService.bulk_import_checks(
        s, agent_id, _import_entries(), overwrite=False, actor_is_admin=True
    )


async def _scan_create(s: AsyncSession, agent_id: UUID) -> None:
    params = _scan_params(agent_id)
    _summary, error = await JobToCheckCoreService._create_new(
        s,
        uuid4(),
        params,
        [(f"ssh_{i}", f"127.0.0.1:{8000 + i}") for i in range(COUNT)],
        JobToCheckCoreService._tcp_builder(params),
        "ssh",
    )
    assert error is None


async def _quick_create(s: AsyncSession, agent_id: UUID) -> None:
    await JobToCheckCoreService.create_single_check(
        s,
        QuickCheckParams(
            agent_id=agent_id,
            check_type="tcp",
            target="127.0.0.1:22",
            display_name="ssh_127_0_0_1",
            interval=60,
            timeout=10,
            retry_attempts=0,
            tags=[],
        ),
    )


WRITERS = {
    "bulk create": _bulk_create,
    "import": _import,
    "scan create": _scan_create,
    "quick create": _quick_create,
}


async def test_two_bulk_creates_at_once_number_the_second_runs_names(
    worker_db, monkeypatch
):
    agent_id = await _agent(worker_db)
    monkeypatch.setattr(
        CheckCRUD,
        "display_names_for_agent",
        staticmethod(_hold_after(CheckCRUD.display_names_for_agent)),
    )

    async def create() -> int:
        async with worker_db() as s:
            result = await CheckCoreService.bulk_create_checks(
                s, agent_id, _bulk_requests()
            )
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

    async def import_() -> tuple[int, int]:
        async with worker_db() as s:
            result = await CheckCoreService.bulk_import_checks(
                s, agent_id, _import_entries(), overwrite=False, actor_is_admin=True
            )
            await s.commit()
            assert result["errors"] == []
            return result["created"], result["skipped"]

    assert sorted(await asyncio.gather(import_(), import_())) == [
        (0, COUNT),
        (COUNT, 0),
    ]

    names = await _names(worker_db, agent_id)
    assert sorted(names) == sorted(e["name"] for e in _import_entries())


@pytest.mark.parametrize("writer", list(WRITERS))
async def test_an_archive_in_flight_refuses_the_writer(worker_db, writer):
    """The archive has locked and archived the agent and not yet committed when
    the writer starts: the writer waits for it, sees it, and makes nothing."""
    agent_id = await _agent(worker_db)
    async with worker_db() as archiver:
        await AgentCoreService.archive_agent(archiver, agent_id)
        await archiver.flush()

        async def write() -> str:
            async with worker_db() as s:
                try:
                    await WRITERS[writer](s, agent_id)
                except ValidationException:
                    await s.rollback()
                    return "refused"
                await s.commit()
                return "written"

        task = asyncio.create_task(write())
        await asyncio.sleep(1)
        await archiver.commit()
    assert await asyncio.wait_for(task, 10) == "refused"
    assert await _names(worker_db, agent_id) == []


@pytest.mark.parametrize("writer", ["bulk create", "import", "scan create"])
async def test_the_agents_requests_go_on_during_a_write(worker_db, monkeypatch, writer):
    """Halfway through the writer's checks, the agent's report admission and
    heartbeat are answered at once; the stamp comes after the last check."""
    agent_id = await _agent(worker_db)
    original = CheckCoreService.create_check
    made = 0
    answered: list[str] = []

    async def create_then_probe(*args: Any, **kwargs: Any) -> Check:
        nonlocal made
        check = await original(*args, **kwargs)
        made += 1
        if made == COUNT // 2:
            async with worker_db() as other:
                admission = await asyncio.wait_for(
                    AgentCRUD.key_share_admission(other, agent_id), HOLD_SECONDS
                )
                assert admission is not None
                answered.append("report")
                await asyncio.wait_for(
                    other.execute(
                        update(Agent)
                        .where(Agent.id == agent_id)
                        .values(hostname="heartbeat")
                    ),
                    HOLD_SECONDS,
                )
                await other.commit()
                answered.append("heartbeat")
        return check

    monkeypatch.setattr(
        CheckCoreService, "create_check", staticmethod(create_then_probe)
    )

    async with worker_db() as s:
        await WRITERS[writer](s, agent_id)
        await s.commit()

    assert answered == ["report", "heartbeat"]
    async with worker_db() as s:
        stamped = (
            await s.execute(select(Agent.checks_updated_at).where(Agent.id == agent_id))
        ).scalar_one()
    assert stamped is not None, "the agent was never told its checks changed"
    assert len(await _names(worker_db, agent_id)) == COUNT


async def test_the_names_hold_waits_for_another_writer_alone(worker_db):
    """A second hold waits; the agent's row stays free."""
    agent_id = await _agent(worker_db)
    async with worker_db() as holder:
        await CheckCRUD.hold_names(holder, agent_id)

        async with worker_db() as other:
            assert await asyncio.wait_for(
                AgentCRUD.key_share_admission(other, agent_id), HOLD_SECONDS
            )
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
