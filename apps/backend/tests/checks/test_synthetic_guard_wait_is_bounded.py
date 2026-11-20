"""A request waits for an agent's synthetic guard at most part of its
statement timeout, then is answered busy.

A long admin write holds the guard of the agent it makes a check synthetic on
for its whole run, every target lookup included: an import carrying a
synthetic entry, an admin's bulk retype to synthetic. A single edit of another
check on that agent, in a request (5 s statement timeout), is refused as busy
(409, sent again later) before that timeout would cut it off as a database
error (500). An import with no synthetic entry takes no guard, and an edit on
another agent is never held.
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
from app.services.core.check_transfer_core_service import CheckTransferCoreService
from fixtures.factories import make_agent, make_check

pytestmark = pytest.mark.integration

FINISH = 40
TARGET = "https://127.0.0.1/"
SLOW_HOST = "slow.example.test"
SLOW_TARGET = f"https://{SLOW_HOST}/"
SCRIPT = (
    "async def run_check(page):\n"
    "    await page.goto('https://127.0.0.1/')\n"
    "    return {'status': 'success', 'steps': ['x']}\n"
)


def _slow_lookups(monkeypatch, seconds: float) -> None:
    """SLOW_HOST resolves to 127.0.0.1 after `seconds`, as a slow resolver."""
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


async def _check(worker_db, agent_id: UUID, name: str, target: str = TARGET) -> UUID:
    async with worker_db() as s:
        check = make_check(
            agent_id=agent_id, display_name=name, check_type="http", target=target
        )
        s.add(check)
        await s.commit()
        return check.id


def _failure(e: Exception) -> str:
    return f"error:{type(e).__name__}:{getattr(e, 'orig', e)!r}"[:300]


async def _editor_edit(worker_db, check_id: UUID) -> tuple[str, float]:
    """An editor's edit of a plain check, in a session with the app's
    5 s statement_timeout (every request session has it)."""
    started = time.monotonic()
    async with worker_db() as s:
        try:
            await CheckCoreService.update_check(
                s, check_id, CheckUpdate(interval_seconds=600), actor_is_admin=False
            )
            await s.commit()
            return "edited", time.monotonic() - started
        except Exception as e:  # the outcome is what is asserted
            await s.rollback()
            return _failure(e), time.monotonic() - started


async def _import(worker_db, agent_id: UUID, entries: list[dict[str, Any]]) -> Any:
    async with worker_db() as s:
        try:
            result = await CheckTransferCoreService.bulk_import_checks(
                s, agent_id, entries, overwrite=True, actor_is_admin=True
            )
            await s.commit()
            return result
        except Exception as e:  # the outcome is what is asserted
            await s.rollback()
            return _failure(e)


async def _edit_during_import(
    worker_db, monkeypatch, *, with_synthetic: bool
) -> dict[str, Any]:
    _slow_lookups(monkeypatch, 2.5)
    agent_id = await _agent(worker_db)
    unrelated = await _check(worker_db, agent_id, "unrelated")
    entries: list[dict[str, Any]] = [
        {"name": f"site-{i}", "check_type": "http", "target": SLOW_TARGET}
        for i in range(3)
    ]
    entries.append(
        {
            "name": "browser",
            "check_type": "synthetic" if with_synthetic else "http",
            "target": TARGET,
            **({"script_code": SCRIPT} if with_synthetic else {}),
        }
    )
    importing = asyncio.create_task(_import(worker_db, agent_id, entries))
    await asyncio.sleep(0.5)  # the import is resolving its entries' targets
    edit, waited = await asyncio.wait_for(_editor_edit(worker_db, unrelated), FINISH)
    return {
        "edit": edit,
        "waited": round(waited, 1),
        "import": await asyncio.wait_for(importing, FINISH),
    }


BUSY = "error:CheckBusyException"
STATEMENT_TIMEOUT_SECONDS = 5


async def test_an_edit_during_an_import_with_a_synthetic_entry_is_busy(
    worker_db, monkeypatch
):
    outcome = await _edit_during_import(worker_db, monkeypatch, with_synthetic=True)
    assert outcome["edit"].startswith(BUSY), outcome
    assert outcome["waited"] < STATEMENT_TIMEOUT_SECONDS, outcome
    assert isinstance(outcome["import"], dict), outcome
    assert outcome["import"]["errors"] == [], outcome


async def test_an_edit_during_an_import_without_a_synthetic_entry_lands(
    worker_db, monkeypatch
):
    outcome = await _edit_during_import(worker_db, monkeypatch, with_synthetic=False)
    assert outcome["edit"] == "edited", outcome
    assert outcome["waited"] < 2, outcome


async def _bulk_retype(worker_db, check_ids: list[UUID]) -> Any:
    async with worker_db() as s:
        try:
            out = await CheckCoreService.bulk_modify(
                s,
                check_ids,
                CheckUpdate(check_type="synthetic", script_code=SCRIPT),
                actor_is_admin=True,
            )
            await s.commit()
            return out
        except Exception as e:  # the outcome is what is asserted
            await s.rollback()
            return _failure(e)


async def test_an_edit_beside_an_admins_bulk_retype_on_its_agent_is_busy(
    worker_db, monkeypatch
):
    _slow_lookups(monkeypatch, 2.5)
    agent_id = await _agent(worker_db)
    unrelated = await _check(worker_db, agent_id, "unrelated")
    selection = [
        await _check(worker_db, agent_id, f"site-{i}", SLOW_TARGET) for i in range(3)
    ]
    retyping = asyncio.create_task(_bulk_retype(worker_db, selection))
    await asyncio.sleep(0.5)
    edit, waited = await asyncio.wait_for(_editor_edit(worker_db, unrelated), FINISH)
    retype = await asyncio.wait_for(retyping, FINISH)
    assert edit.startswith(BUSY), (edit, round(waited, 1), retype)
    assert waited < STATEMENT_TIMEOUT_SECONDS, (edit, round(waited, 1), retype)
    assert isinstance(retype, dict) and retype["success_count"] == 3, retype


async def test_an_edit_on_another_agent_beside_a_bulk_retype_lands(
    worker_db, monkeypatch
):
    _slow_lookups(monkeypatch, 2.5)
    agent_id = await _agent(worker_db)
    other_agent = await _agent(worker_db)
    unrelated = await _check(worker_db, other_agent, "unrelated")
    selection = [
        await _check(worker_db, agent_id, f"site-{i}", SLOW_TARGET) for i in range(3)
    ]
    retyping = asyncio.create_task(_bulk_retype(worker_db, selection))
    await asyncio.sleep(0.5)
    edit, waited = await asyncio.wait_for(_editor_edit(worker_db, unrelated), FINISH)
    retype = await asyncio.wait_for(retyping, FINISH)
    assert edit == "edited" and waited < 2, (edit, round(waited, 1), retype)
