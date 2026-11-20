"""The synthetic guard and the rows a judged write holds, driven through the
real entry points (admin API, web form, MCP) beside a running maintenance job
(process_next_job).

Pointing a check at a parent the job is writing lands (the parent is held FOR
KEY SHARE, what the foreign key reads), as does an edit of another field; an
edit on an agent whose guard the job holds is answered busy (409) before the
request's statement timeout, and the job still finishes.
"""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from app.background.maintenance_handlers import register_all
from app.background.maintenance_worker import process_next_job
from app.core import rate_limit
from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from app.mcp.server import MCPAuthMiddleware, mcp_server
from app.models.check_model import Check
from app.models.enum_model import MaintenanceJobKind, MaintenanceJobStatus
from app.models.maintenance_job_model import MaintenanceJob
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.api_token_core_service import ApiTokenCoreService
from app.services.core.auth_core_service import AuthCoreService
from fixtures.factories import make_agent, make_check, make_user
from httpx import ASGITransport
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from sqlalchemy import select, update
from starlette.applications import Starlette
from starlette.routing import Mount

pytestmark = pytest.mark.integration

TARGET = "https://127.0.0.1/"
SLOW_HOST = "slow.example.test"
SLOW_TARGET = f"https://{SLOW_HOST}/"
SCRIPT = (
    "async def run_check(page):\n"
    "    await page.goto('https://127.0.0.1/')\n"
    "    return {'status': 'success', 'steps': ['x']}\n"
)
MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
FINISH = 60


def _slow_lookups(monkeypatch, seconds: float) -> None:
    real = socket.getaddrinfo

    def resolve(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host != SLOW_HOST:
            return real(host, *args, **kwargs)
        time.sleep(seconds)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


async def _token(maker, role: str) -> str:
    async with maker() as s:
        user = make_user(role=role)
        s.add(user)
        await s.flush()
        _, plain = await ApiTokenCoreService.create_token(
            s, user.id, ApiTokenCreate(name=f"r11b-{uuid4().hex[:6]}", scopes=["*"])
        )
        await s.commit()
    return plain


async def _user(maker, role: str) -> UUID:
    async with maker() as s:
        user = make_user(role=role)
        s.add(user)
        await s.commit()
        return user.id


async def _agent(maker) -> UUID:
    async with maker() as s:
        agent = make_agent()
        s.add(agent)
        await s.commit()
        return agent.id


async def _check(maker, agent_id: UUID, name: str) -> UUID:
    async with maker() as s:
        check = make_check(
            agent_id=agent_id, display_name=name, check_type="http", target=TARGET
        )
        s.add(check)
        await s.commit()
        return check.id


async def _queue_import(maker, agent_id: UUID, owner: UUID, entries) -> UUID:
    job = MaintenanceJob(
        id=uuid4(),
        kind=MaintenanceJobKind.BULK_CHECK_IMPORT,
        target_id=None,
        params={"agent_id": str(agent_id), "mode": "replace", "checks": entries},
        owner_id=owner,
        status=MaintenanceJobStatus.QUEUED,
    )
    async with maker() as s:
        s.add(job)
        await s.commit()
    return job.id


async def _job(maker, job_id: UUID) -> MaintenanceJob:
    async with maker() as s:
        return (
            await s.execute(select(MaintenanceJob).where(MaintenanceJob.id == job_id))
        ).scalar_one()


async def _stored(maker, check_id: UUID) -> Check:
    async with maker() as s:
        return (await s.execute(select(Check).where(Check.id == check_id))).scalar_one()


@asynccontextmanager
async def _api() -> AsyncIterator[httpx.AsyncClient]:
    limiter.reset()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as c:
        yield c
    limiter.reset()


@asynccontextmanager
async def _mcp() -> AsyncIterator[httpx.AsyncClient]:
    rate_limit.credential_attempts.reset()
    rate_limit.mcp_token_calls.reset()
    manager = StreamableHTTPSessionManager(
        app=mcp_server, json_response=True, stateless=True
    )
    mcp_app = Starlette(
        routes=[Mount("/mcp", app=MCPAuthMiddleware(StreamableHTTPASGIApp(manager)))]
    )
    async with (
        manager.run(),
        httpx.AsyncClient(
            transport=ASGITransport(app=mcp_app), base_url="http://t"
        ) as c,
    ):
        yield c


async def _scenario(worker_db, monkeypatch, *, synthetic_entry: bool):
    """Agent A has parent P; agent B has child C. An admin's import onto A
    (a real queued job, mode replace) overwrites P first, then makes three
    checks whose targets resolve slowly (2.5 s each)."""
    _slow_lookups(monkeypatch, 2.5)
    a = await _agent(worker_db)
    b = await _agent(worker_db)
    p = await _check(worker_db, a, "parent")
    c = await _check(worker_db, b, "child")
    admin = await _user(worker_db, "admin")
    entries: list[dict[str, Any]] = [
        {"name": "parent", "check_type": "http", "target": TARGET, "interval": 120}
    ]
    entries += [
        {"name": f"site-{i}", "check_type": "http", "target": SLOW_TARGET}
        for i in range(3)
    ]
    if synthetic_entry:
        entries.append(
            {
                "name": "browser",
                "check_type": "synthetic",
                "target": TARGET,
                "script_code": SCRIPT,
            }
        )
    job_id = await _queue_import(worker_db, a, admin, entries)
    return a, b, p, c, job_id


async def _run_job_beside(call):
    """Start the worker on the queued import, then make `call` 1 s in."""
    register_all()
    worker = asyncio.create_task(process_next_job())
    await asyncio.sleep(1.0)
    started = time.monotonic()
    result = await asyncio.wait_for(call(), FINISH)
    waited = round(time.monotonic() - started, 1)
    await asyncio.wait_for(worker, FINISH)
    return result, waited


def _mcp_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


# ── F1: a child pointed at a parent an import (no synthetic entry) is writing ──


async def test_api_patch_pointing_child_at_parent_beside_import(worker_db, monkeypatch):
    """Base answered at once (the FK takes KEY SHARE on the parent, which the
    import's NO KEY UPDATE does not block). Now the edit holds the parent FOR
    NO KEY UPDATE, waits on the import, and the statement timeout ends it."""
    _a, _b, p, c, job_id = await _scenario(
        worker_db, monkeypatch, synthetic_entry=False
    )
    editor = await _token(worker_db, "editor")

    async with _api() as client:

        async def call():
            return await client.patch(
                f"/api/v1/admin/checks/{c}",
                headers={"Authorization": f"Bearer {editor}"},
                json={"depends_on_check_id": str(p)},
            )

        r, waited = await _run_job_beside(call)
    job = await _job(worker_db, job_id)
    stored = await _stored(worker_db, c)
    outcome = (
        r.status_code,
        waited,
        r.text[:200],
        job.status,
        stored.depends_on_check_id,
    )
    assert r.status_code == 200, outcome
    assert waited < 2, outcome


async def test_api_put_dependents_beside_import(worker_db, monkeypatch):
    _a, _b, p, c, job_id = await _scenario(
        worker_db, monkeypatch, synthetic_entry=False
    )
    editor = await _token(worker_db, "editor")

    async with _api() as client:

        async def call():
            return await client.put(
                f"/api/v1/admin/checks/{p}/dependents",
                headers={"Authorization": f"Bearer {editor}"},
                json={"dependent_ids": [str(c)]},
            )

        r, waited = await _run_job_beside(call)
    job = await _job(worker_db, job_id)
    outcome = (r.status_code, waited, r.text[:200], job.status)
    assert r.status_code == 200, outcome
    assert waited < 2, outcome


async def _web_client(maker, role: str) -> httpx.AsyncClient:
    async with maker() as s:
        user = make_user(role=role)
        s.add(user)
        await s.flush()
        _, token = await AuthCoreService().create_session(s, user.id)
        await s.commit()
    c = httpx.AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://test",
    )
    c.cookies.set(settings.security.session_cookie_name, token)
    return c


async def test_web_dependents_form_beside_import(worker_db, monkeypatch):
    _a, _b, p, c, job_id = await _scenario(
        worker_db, monkeypatch, synthetic_entry=False
    )
    client = await _web_client(worker_db, "editor")
    async with client:

        async def call():
            return await client.post(
                f"/checks/{p}/dependents",
                data={"dependent_ids": [str(c)]},
                headers={"HX-Request": "true"},
            )

        r, waited = await _run_job_beside(call)
    stored = await _stored(worker_db, c)
    outcome = (r.status_code, waited, r.text[:200], stored.depends_on_check_id)
    assert r.status_code == 200, outcome
    assert waited < 2, outcome


async def test_fk_only_write_beside_import_lands(worker_db, monkeypatch):
    """What the base's write did: point C at P by UPDATE alone (the FK reads P
    FOR KEY SHARE). It is not held by the import's hold on P."""
    _a, _b, p, c, _job_id = await _scenario(
        worker_db, monkeypatch, synthetic_entry=False
    )

    async def call():
        async with worker_db() as s:
            await s.execute(
                update(Check).where(Check.id == c).values(depends_on_check_id=p)
            )
            await s.commit()
        return "written"

    r, waited = await _run_job_beside(call)
    assert r == "written" and waited < 2, (r, waited)
    assert (await _stored(worker_db, c)).depends_on_check_id == p


async def test_mcp_update_check_pointing_at_parent_beside_import(
    worker_db, monkeypatch
):
    _a, _b, p, c, job_id = await _scenario(
        worker_db, monkeypatch, synthetic_entry=False
    )
    editor = await _token(worker_db, "editor")

    async with _mcp() as client:

        async def call():
            return await client.post(
                "/mcp/",
                headers={**MCP_HEADERS, "Authorization": f"Bearer {editor}"},
                json=_mcp_call(
                    "update_check",
                    {"check_id": str(c), "changes": {"depends_on_check_id": str(p)}},
                ),
            )

        r, waited = await _run_job_beside(call)
    body = r.json()
    outcome = (r.status_code, waited, str(body)[:300])
    assert body["result"]["isError"] is False, outcome
    assert waited < 2, outcome


# ── Controls ────────────────────────────────────────────────────────────────


async def test_api_patch_interval_beside_import_lands(worker_db, monkeypatch):
    _a, _b, _p, c, job_id = await _scenario(
        worker_db, monkeypatch, synthetic_entry=False
    )
    editor = await _token(worker_db, "editor")

    async with _api() as client:

        async def call():
            return await client.patch(
                f"/api/v1/admin/checks/{c}",
                headers={"Authorization": f"Bearer {editor}"},
                json={"interval_seconds": 300},
            )

        r, waited = await _run_job_beside(call)
    job = await _job(worker_db, job_id)
    outcome = (r.status_code, waited, r.text[:200], job.status)
    assert r.status_code == 200 and waited < 2, outcome
    assert job.status == MaintenanceJobStatus.DONE, outcome


async def test_guard_busy_end_to_end_api_and_job(worker_db, monkeypatch):
    """The guard's designed answer through the real stack: an editor's edit
    of a check on the agent an import with a synthetic entry runs on is 409
    within the statement timeout, nothing changed; the job (statement_timeout
    0) completes."""
    a, _b, _p, _c, job_id = await _scenario(
        worker_db, monkeypatch, synthetic_entry=True
    )
    other = await _check(worker_db, a, "other-on-a")
    editor = await _token(worker_db, "editor")

    async with _api() as client:

        async def call():
            return await client.patch(
                f"/api/v1/admin/checks/{other}",
                headers={"Authorization": f"Bearer {editor}"},
                json={"interval_seconds": 300},
            )

        r, waited = await _run_job_beside(call)
    job = await _job(worker_db, job_id)
    stored = await _stored(worker_db, other)
    outcome = (r.status_code, waited, r.text[:200], job.status, job.progress)
    assert r.status_code == 409 and "CHECK_BUSY" in r.text, outcome
    assert waited < 5, outcome
    assert stored.interval_seconds == 60, outcome
    assert job.status == MaintenanceJobStatus.DONE, outcome


async def test_guard_busy_end_to_end_mcp(worker_db, monkeypatch):
    a, _b, _p, _c, _job_id = await _scenario(
        worker_db, monkeypatch, synthetic_entry=True
    )
    other = await _check(worker_db, a, "other-on-a")
    editor = await _token(worker_db, "editor")

    async with _mcp() as client:

        async def call():
            return await client.post(
                "/mcp/",
                headers={**MCP_HEADERS, "Authorization": f"Bearer {editor}"},
                json=_mcp_call("disable_check", {"check_id": str(other)}),
            )

        r, waited = await _run_job_beside(call)
    body = r.json()
    stored = await _stored(worker_db, other)
    outcome = (r.status_code, waited, str(body)[:300])
    assert body["result"]["isError"] is True, outcome
    assert "being changed by another request" in str(body), outcome
    assert stored.enabled is True, outcome
