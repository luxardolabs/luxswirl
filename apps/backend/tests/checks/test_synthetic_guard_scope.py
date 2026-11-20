"""What the synthetic guard covers, and what it leaves free.

An import job makes no synthetic check here, so it holds no guard: a web edit
of a check on the same agent is answered while it runs, as is an edit of
another check of the agent the import does not touch. An admin's bulk retype
to synthetic of a check an editor is moving to another agent never leaves an
editor's edit on a synthetic check: the retype is refused as busy, or the
editor's edit is refused.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import pytest
from app.core.exceptions import AuthorizationException
from app.models.check_model import Check
from app.schemas.check_schema import CheckUpdate
from app.services.core.check_core_service import CheckCoreService
from app.services.core.check_transfer_core_service import CheckTransferCoreService
from fixtures.factories import make_agent, make_check
from sqlalchemy import select, text

pytestmark = pytest.mark.integration

PAUSE = 1.5
FINISH = 30
TARGET = "https://127.0.0.1/"
SCRIPT = (
    "async def run_check(page):\n"
    "    await page.goto('https://127.0.0.1/')\n"
    "    return {'status': 'success', 'steps': ['x']}\n"
)


async def _agent(worker_db) -> UUID:
    async with worker_db() as s:
        agent = make_agent()
        s.add(agent)
        await s.commit()
        return agent.id


async def _check(worker_db, agent_id: UUID, **kw: Any) -> UUID:
    async with worker_db() as s:
        check = make_check(agent_id=agent_id, **kw)
        s.add(check)
        await s.commit()
        return check.id


def _failure(e: Exception) -> str:
    return f"error:{type(e).__name__}:{getattr(e, 'orig', e)!r}"[:300]


def _slow_create(monkeypatch, *, before: float = 0.0, after_first: float = 0.0):
    real = CheckCoreService.create_check
    calls = {"n": 0}

    async def slow(*args: Any, **kwargs: Any):
        calls["n"] += 1
        if before:
            await asyncio.sleep(before)
        out = await real(*args, **kwargs)
        if after_first and calls["n"] == 1:
            await asyncio.sleep(after_first)
        return out

    monkeypatch.setattr(CheckCoreService, "create_check", staticmethod(slow))


async def _import(worker_db, agent_id: UUID, entries: list[dict[str, Any]]) -> str:
    async with worker_db() as s:
        try:
            # As the maintenance worker runs it.
            await s.execute(text("SET LOCAL statement_timeout = 0"))
            await s.execute(text("SET LOCAL idle_in_transaction_session_timeout = 0"))
            out = await CheckTransferCoreService.bulk_import_checks(
                s, agent_id, entries, overwrite=True, actor_is_admin=True
            )
            await s.commit()
            return f"import:{out['created']}c/{out['updated']}u:{out['errors']}"
        except Exception as e:  # the outcome is what is asserted
            await s.rollback()
            return _failure(e)


async def _editor_edit(worker_db, check_id: UUID, data: CheckUpdate) -> str:
    async with worker_db() as s:
        try:
            await CheckCoreService.update_check(s, check_id, data, actor_is_admin=False)
            await s.commit()
            return "edited"
        except AuthorizationException:
            await s.rollback()
            return "refused"
        except Exception as e:  # the outcome is what is asserted
            await s.rollback()
            return _failure(e)


def _tcp(name: str, port: int) -> dict[str, Any]:
    return {"name": name, "check_type": "tcp", "target": f"127.0.0.1:{port}"}


# An import beside an editor's edit of another check.


async def _import_vs_edit(worker_db, monkeypatch, *, edit_overwritten: bool):
    agent_id = await _agent(worker_db)
    overwritten = await _check(
        worker_db,
        agent_id,
        display_name="kept",
        check_type="tcp",
        target="127.0.0.1:9001",
    )
    other = await _check(
        worker_db,
        agent_id,
        display_name="other",
        check_type="tcp",
        target="127.0.0.1:9002",
    )
    _slow_create(monkeypatch, before=PAUSE)
    entries = [_tcp("kept", 9101), _tcp("new-one", 9102)]
    importing = asyncio.create_task(_import(worker_db, agent_id, entries))
    await asyncio.sleep(0.5)  # entry 1 updated and holds its row; entry 2 pausing
    editing = asyncio.create_task(
        _editor_edit(
            worker_db,
            overwritten if edit_overwritten else other,
            CheckUpdate(interval_seconds=600),
        )
    )
    return {
        "import": await asyncio.wait_for(importing, FINISH),
        "edit": await asyncio.wait_for(editing, FINISH),
    }


async def test_an_import_and_an_edit_of_another_check_both_finish(
    worker_db, monkeypatch
):
    outcome = await _import_vs_edit(worker_db, monkeypatch, edit_overwritten=False)
    assert outcome == {"import": "import:1c/1u:[]", "edit": "edited"}, outcome


# A long import job beside a web edit on the same agent.


async def _long_import_vs_edit(worker_db, monkeypatch, *, same_agent: bool):
    agent_id = await _agent(worker_db)
    other_agent = await _agent(worker_db)
    edited = await _check(
        worker_db,
        agent_id if same_agent else other_agent,
        display_name="unrelated",
        check_type="tcp",
        target="127.0.0.1:9003",
    )
    # The import's first check made, then the rest of a long import (7 s).
    _slow_create(monkeypatch, after_first=7)
    entries = [_tcp("a", 9201), _tcp("b", 9202)]
    importing = asyncio.create_task(_import(worker_db, agent_id, entries))
    await asyncio.sleep(1)
    editing = asyncio.create_task(
        _editor_edit(worker_db, edited, CheckUpdate(interval_seconds=600))
    )
    return {
        "import": await asyncio.wait_for(importing, FINISH),
        "edit": await asyncio.wait_for(editing, FINISH),
    }


async def test_a_web_edit_on_an_agent_is_not_held_by_its_import_job(
    worker_db, monkeypatch
):
    outcome = await _long_import_vs_edit(worker_db, monkeypatch, same_agent=True)
    assert outcome["edit"] == "edited", outcome


async def test_a_web_edit_on_another_agent_during_an_import_is_answered(
    worker_db, monkeypatch
):
    outcome = await _long_import_vs_edit(worker_db, monkeypatch, same_agent=False)
    assert outcome == {"import": "import:2c/0u:[]", "edit": "edited"}, outcome


# An admin's bulk retype to synthetic of a check an editor is moving.


async def _held_bulk(
    worker_db,
    check_ids: list[UUID],
    data: CheckUpdate,
    *,
    new_agent_id: UUID | None,
    actor_is_admin: bool,
    hold: float,
) -> str:
    async with worker_db() as s:
        try:
            await s.execute(text("SET LOCAL statement_timeout = 0"))
            out = await CheckCoreService.bulk_modify(
                s, check_ids, data, new_agent_id, actor_is_admin=actor_is_admin
            )
            await s.flush()
            await asyncio.sleep(hold)
            await s.commit()
            return f"bulk:{out['success_count']}/{out['failure_count']}:{out['errors']}"
        except Exception as e:  # the outcome is what is asserted
            await s.rollback()
            return _failure(e)


async def _stored(worker_db, check_id: UUID) -> tuple[Any, ...]:
    async with worker_db() as s:
        row = (
            await s.execute(
                select(Check.agent_id, Check.check_type, Check.enabled).where(
                    Check.id == check_id
                )
            )
        ).one()
        return tuple(row)


async def _retype_race(worker_db, *, moved: bool):
    a = await _agent(worker_db)
    b = await _agent(worker_db)
    check_id = await _check(
        worker_db,
        b if not moved else a,
        display_name="browse",
        check_type="http",
        target=TARGET,
    )
    retype = CheckUpdate(check_type="synthetic", script_code=SCRIPT, target=TARGET)
    tasks: dict[str, asyncio.Task[str]] = {}
    if moved:
        # An editor's bulk move of the plain check from A to B, held open.
        tasks["move"] = asyncio.create_task(
            _held_bulk(
                worker_db,
                [check_id],
                CheckUpdate(),
                new_agent_id=b,
                actor_is_admin=False,
                hold=PAUSE,
            )
        )
        await asyncio.sleep(0.5)
    # The admin's bulk retype to synthetic, held open after its write.
    tasks["retype"] = asyncio.create_task(
        _held_bulk(
            worker_db,
            [check_id],
            retype,
            new_agent_id=None,
            actor_is_admin=True,
            hold=2 * PAUSE,
        )
    )
    await asyncio.sleep(PAUSE + 0.5)  # the move committed, the retype written
    tasks["edit"] = asyncio.create_task(
        _editor_edit(worker_db, check_id, CheckUpdate(enabled=False))
    )
    outcome = {k: await asyncio.wait_for(t, FINISH) for k, t in tasks.items()}
    agent_id, check_type, enabled = await _stored(worker_db, check_id)
    outcome["stored"] = f"{'B' if agent_id == b else 'A'}:{check_type}:{enabled}"
    return outcome


async def test_an_editors_edit_never_changes_a_check_retyped_while_moving(worker_db):
    outcome = await _retype_race(worker_db, moved=True)
    if outcome["stored"].startswith("B:synthetic"):
        assert outcome["stored"] == "B:synthetic:True", outcome
        assert outcome["edit"] == "refused", outcome
    else:
        # The retype saw the check moved under it and changed nothing.
        assert "being changed by another request" in outcome["retype"], outcome
        assert outcome["stored"] == "B:http:False", outcome


async def test_an_editors_edit_waits_for_a_retype_in_place_and_is_refused(worker_db):
    outcome = await _retype_race(worker_db, moved=False)
    assert outcome["stored"] == "B:synthetic:True", outcome
    assert outcome["edit"] == "refused", outcome
