"""An import that updates an existing check and then creates one, beside an
editor's edit or an admin's delete of the updated check: none deadlocks."""

from __future__ import annotations

import asyncio
import socket
import time
from typing import Any
from uuid import UUID

import pytest
from app.crud.check_crud import CheckCRUD
from app.schemas.check_schema import CheckUpdate
from app.services.core.check_core_service import CheckCoreService
from fixtures.factories import make_agent, make_check

pytestmark = pytest.mark.integration

FINISH_SECONDS = 20
TARGET = "https://127.0.0.1/"


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


def _pause_get_as_stored(
    monkeypatch, check_id: UUID
) -> tuple[asyncio.Event, asyncio.Event]:
    """The first read of `check_id` as stored (taken under the guard) waits for
    `release`; `holding` is set when it starts waiting."""
    real = CheckCRUD.get_as_stored
    holding, release = asyncio.Event(), asyncio.Event()
    state = {"paused": False}

    async def paused(db, wanted: UUID, **kw: Any):
        if wanted == check_id and not state["paused"]:
            state["paused"] = True
            holding.set()
            await release.wait()
        return await real(db, wanted, **kw)

    monkeypatch.setattr(CheckCRUD, "get_as_stored", staticmethod(paused))
    return holding, release


async def _editor_edit(worker_db, check_id: UUID) -> str:
    async with worker_db() as s:
        try:
            await CheckCoreService.update_check(
                s, check_id, CheckUpdate(interval_seconds=600), actor_is_admin=False
            )
            await s.commit()
            return "edited"
        except Exception as e:  # the outcome is what is asserted
            await s.rollback()
            return _failure(e)


async def _import(worker_db, agent_id: UUID, entries: list[dict[str, Any]]) -> Any:
    async with worker_db() as s:
        try:
            result = await CheckCoreService.bulk_import_checks(
                s, agent_id, entries, overwrite=True, actor_is_admin=True
            )
            await s.commit()
            return result
        except Exception as e:  # the outcome is what is asserted
            await s.rollback()
            return _failure(e)


async def _race_edit_and_import(
    worker_db, monkeypatch, *, update_first: bool
) -> dict[str, Any]:
    agent_id = await _agent(worker_db)
    edited = await _check(
        worker_db, agent_id, display_name="edited", check_type="http", target=TARGET
    )
    holding, release = _pause_get_as_stored(monkeypatch, edited)

    editing = asyncio.create_task(_editor_edit(worker_db, edited))
    await asyncio.wait_for(holding.wait(), 10)  # the editor holds the guard

    update_entry = {
        "name": "edited",
        "check_type": "http",
        "target": TARGET,
        "interval": 120,
    }
    create_entry = {"name": "fresh", "check_type": "http", "target": TARGET}
    entries = (
        [update_entry, create_entry] if update_first else [create_entry, update_entry]
    )
    importing = asyncio.create_task(_import(worker_db, agent_id, entries))
    await asyncio.sleep(1.5)  # the import is as far as the guard lets it
    release.set()
    return {
        "edit": await asyncio.wait_for(editing, FINISH_SECONDS),
        "import": await asyncio.wait_for(importing, FINISH_SECONDS),
    }


async def test_an_import_creating_first_does_not_deadlock_with_an_edit(
    worker_db, monkeypatch
):
    outcome = await _race_edit_and_import(worker_db, monkeypatch, update_first=False)
    assert "deadlock" not in str(outcome).lower(), outcome
    assert outcome["edit"] == "edited", outcome
    assert outcome["import"]["errors"] == [], outcome


async def test_import_updating_then_creating_does_not_deadlock_with_an_edit(
    worker_db, monkeypatch
):
    outcome = await _race_edit_and_import(worker_db, monkeypatch, update_first=True)
    assert "deadlock" not in str(outcome).lower(), outcome
    assert outcome["edit"] == "edited", outcome
    assert outcome["import"]["errors"] == [], outcome


SLOW_HOST = "slow.example.test"


def _slow_lookups(monkeypatch, seconds: float) -> None:
    """SLOW_HOST resolves to 127.0.0.1 after `seconds`, as a slow resolver."""
    real = socket.getaddrinfo

    def resolve(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host != SLOW_HOST:
            return real(host, *args, **kwargs)
        time.sleep(seconds)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


async def _editor_delete(worker_db, check_id: UUID) -> str:
    async with worker_db() as s:
        try:
            await CheckCoreService.delete_check(s, check_id, actor_is_admin=False)
            await s.commit()
            return "deleted"
        except Exception as e:  # the outcome is what is asserted
            await s.rollback()
            return _failure(e)


async def _import_then_delete(
    worker_db, monkeypatch, *, with_create: bool
) -> dict[str, Any]:
    """No lock seam patched: the import updates "edited", then resolves a new
    target for "other" (slowly), then (with_create) creates "fresh". An editor
    deletes "edited" meanwhile, as any user might while an import runs."""
    _slow_lookups(monkeypatch, 3.0)
    agent_id = await _agent(worker_db)
    edited = await _check(
        worker_db, agent_id, display_name="edited", check_type="http", target=TARGET
    )
    await _check(
        worker_db, agent_id, display_name="other", check_type="http", target=TARGET
    )
    entries: list[dict[str, Any]] = [
        {"name": "edited", "check_type": "http", "target": TARGET, "interval": 120},
        {"name": "other", "check_type": "http", "target": f"https://{SLOW_HOST}/"},
    ]
    if with_create:
        entries.append({"name": "fresh", "check_type": "http", "target": TARGET})
    importing = asyncio.create_task(_import(worker_db, agent_id, entries))
    await asyncio.sleep(1.0)  # the import is resolving "other"'s new target
    deleting = asyncio.create_task(_editor_delete(worker_db, edited))
    return {
        "import": await asyncio.wait_for(importing, FINISH_SECONDS),
        "delete": await asyncio.wait_for(deleting, FINISH_SECONDS),
    }


async def test_a_delete_of_a_check_an_import_already_updated_does_not_deadlock(
    worker_db, monkeypatch
):
    outcome = await _import_then_delete(worker_db, monkeypatch, with_create=True)
    assert "deadlock" not in str(outcome).lower(), outcome
    assert outcome["delete"] == "deleted", outcome
    assert outcome["import"]["errors"] == [], outcome


async def test_a_delete_during_an_import_that_only_updates_finishes(
    worker_db, monkeypatch
):
    outcome = await _import_then_delete(worker_db, monkeypatch, with_create=False)
    assert "deadlock" not in str(outcome).lower(), outcome
    assert outcome["delete"] == "deleted", outcome
    assert outcome["import"]["errors"] == [], outcome
