"""What archiving an agent must not let happen elsewhere.

An archived agent is kept by the stale-agent cleanup, moved out of archived
only by restore, left out of the dashboard counts as it is out of the rows,
not exported as a down agent on /metrics, never left behind by a migration
downgrade for earlier code to misread, and not rewritten by a heartbeat sent
with the shared registration key. Each case sits beside a control that shows
the same path still works for an agent that is not archived.
"""

from __future__ import annotations

import contextlib
import importlib.util
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.exceptions import ValidationException
from app.core.rate_limit import limiter
from app.crud.agent_crud import AgentCRUD
from app.crud.check_result_crud import CheckResultCRUD
from app.main import app
from app.models.agent_model import Agent
from app.models.base_model import str_enum
from app.schemas.registration_key_schema import RegistrationKeyCreate
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.metrics_collector_core_service import (
    MetricsCollectorCoreService,
    agent_label,
    agent_up,
)
from app.services.core.registration_key_core_service import RegistrationKeyCoreService
from fixtures.factories import make_agent, make_check
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text, type_coerce
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

_OLD = datetime(2000, 1, 1, tzinfo=UTC)


async def _agent(db: AsyncSession, **overrides: Any) -> Agent:
    agent = make_agent(**overrides)
    db.add(agent)
    await db.flush()
    return agent


async def _status(db: AsyncSession, agent_id: UUID) -> str:
    db.expire_all()
    return (
        await db.execute(select(Agent.approval_status).where(Agent.id == agent_id))
    ).scalar_one()


# --- the stale-agent cleanup keeps an archived agent and its history --------


async def test_stale_cleanup_deletes_a_stale_disabled_agent(
    db: AsyncSession,
) -> None:
    stale_id = (await _agent(db, approval_status="disabled", last_seen=_OLD)).id
    await AgentCRUD.delete_stale_agents(db, utc_now() - timedelta(days=30))
    db.expire_all()
    assert (await db.execute(select(Agent).where(Agent.id == stale_id))).first() is None


async def test_stale_cleanup_keeps_an_archived_agent(db: AsyncSession) -> None:
    """Refused heartbeats, an archived agent's last_seen only grows older; the
    cleanup_stale_agents job must not delete it (and its checks and results)."""
    archived_id = (await _agent(db, approval_status="archived", last_seen=_OLD)).id
    names = await AgentCRUD.get_stale_agent_names(db, utc_now() - timedelta(days=30))
    await AgentCRUD.delete_stale_agents(db, utc_now() - timedelta(days=30))
    db.expire_all()
    assert (
        await db.execute(select(Agent).where(Agent.id == archived_id))
    ).first() is not None, "archived agent was deleted by the stale-agent cleanup"
    assert names == []


# --- only restore moves an agent out of archived ----------------------------


async def test_restore_is_the_documented_exit(db: AsyncSession) -> None:
    agent = await _agent(db, approval_status="archived")
    await AgentCoreService.restore_agent(db, agent.id)
    assert await _status(db, agent.id) == "disabled"


@pytest.mark.parametrize(
    "transition",
    [
        "resume_agent",
        "enable_agent",
        "approve_agent",
        "pause_agent",
        "reject_agent",
        "disable_agent",
    ],
)
async def test_only_restore_moves_an_archived_agent(
    db: AsyncSession, transition: str
) -> None:
    agent = await _agent(db, approval_status="archived")
    # approve_agent also judges who approves; an admin is the widest reach.
    extra = {"actor_is_admin": True} if transition == "approve_agent" else {}
    with contextlib.suppress(ValidationException, ValueError):
        await getattr(AgentCoreService, transition)(db, agent.id, **extra)
    assert await _status(db, agent.id) == "archived", (
        f"{transition} moved an archived agent out of archived"
    )


# --- the dashboard summary counts the checks its rows list --------------------


async def test_summary_matches_rows_without_archived(db: AsyncSession) -> None:
    disabled = await _agent(db, approval_status="disabled")
    db.add(make_check(agent_id=disabled.id, enabled=True))
    await db.flush()
    summary = await CheckResultCRUD.get_status_summary_data(db)
    _, total = await CheckResultCRUD.get_checks_with_agents_filtered(db)
    assert summary["total_checks"] == total


async def test_summary_matches_rows_with_an_archived_agent(
    db: AsyncSession,
) -> None:
    disabled = await _agent(db, approval_status="disabled")
    archived = await _agent(db, approval_status="archived")
    db.add(make_check(agent_id=disabled.id, enabled=True))
    db.add(make_check(agent_id=archived.id, enabled=True))
    await db.flush()
    summary = await CheckResultCRUD.get_status_summary_data(db)
    _, total = await CheckResultCRUD.get_checks_with_agents_filtered(db)
    assert summary["enabled_checks"] == total, (
        f"summary card counts {summary['enabled_checks']} enabled checks "
        f"(the archived one as 'unknown'), the rows show {total}"
    )


# --- /metrics does not export an archived agent as down ----------------------


def _agent_up_value(label: str) -> float | None:
    for metric in agent_up.collect():
        for sample in metric.samples:
            if sample.labels.get("agent") == label:
                return sample.value
    return None


async def test_metrics_export_a_live_agent_as_up(db: AsyncSession) -> None:
    live = await _agent(db, approval_status="active")
    await MetricsCollectorCoreService.refresh_agent_status(db)
    assert _agent_up_value(agent_label(live.agent_name, live.id)) == 1


async def test_metrics_do_not_export_an_archived_agent_as_down(
    db: AsyncSession,
) -> None:
    archived = await _agent(db, approval_status="archived", last_seen=_OLD)
    await MetricsCollectorCoreService.refresh_agent_status(db)
    value = _agent_up_value(agent_label(archived.agent_name, archived.id))
    assert value is None, (
        f"luxswirl_agent_up={value} for an archived agent: an agent_up==0 "
        "Prometheus alert fires for it on every scrape"
    )


# --- a downgrade leaves no status the earlier code cannot read ---------------

_REVISION = (
    Path(__file__).resolve().parent.parent.parent
    / "alembic"
    / "versions"
    / "006_agent_archived_status.py"
)


class _PreviousStatus(StrEnum):
    """AgentApprovalStatus as of the parent commit."""

    PENDING = "pending"
    ACTIVE = "active"
    PAUSED = "paused"
    DISABLED = "disabled"
    REJECTED = "rejected"


def _downgrade(connection: Any) -> None:
    spec = importlib.util.spec_from_file_location("rev006", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        module.downgrade()


async def test_previous_code_reads_a_disabled_row(db: AsyncSession) -> None:
    agent = await _agent(db, approval_status="disabled")
    await db.run_sync(lambda s: _downgrade(s.connection()))
    value = (
        await db.execute(
            select(
                type_coerce(Agent.approval_status, str_enum(_PreviousStatus, 20))
            ).where(Agent.id == agent.id)
        )
    ).scalar_one()
    assert value == _PreviousStatus.DISABLED


async def test_downgrade_leaves_no_archived_rows(db: AsyncSession) -> None:
    agent = await _agent(db, approval_status="archived")
    await db.run_sync(lambda s: _downgrade(s.connection()))
    left = (
        await db.execute(
            select(func.count())
            .select_from(Agent)
            .where(text("approval_status = 'archived'"))
        )
    ).scalar_one()
    try:
        await db.execute(
            select(
                type_coerce(Agent.approval_status, str_enum(_PreviousStatus, 20))
            ).where(Agent.id == agent.id)
        )
        read_error = None
    except LookupError as exc:
        read_error = repr(exc)
    assert left == 0, (
        f"{left} archived row(s) survive downgrade; previous code's read: {read_error}"
    )


# --- the shared registration key does not rewrite an archived agent ---------


@pytest_asyncio.fixture
async def client(db: AsyncSession, monkeypatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    limiter.reset()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _reg_key_heartbeat_onto_archived(
    client: AsyncClient, db: AsyncSession, *, revoke: bool
) -> None:
    _, reg_token = await RegistrationKeyCoreService.create_key(
        db, RegistrationKeyCreate(name="adv"), created_by="tests"
    )
    agent = await _agent(db, approval_status="pending", last_seen=_OLD)
    agent_id = agent.id
    await AgentCoreService.approve_agent(db, agent_id, actor_is_admin=True)
    await AgentCoreService.archive_agent(db, agent_id)
    if revoke:
        await AgentCoreService.revoke_agent_key(db, agent_id)

    resp = await client.post(
        "/api/v1/agent/heartbeat",
        json={
            "agent_id": str(agent_id),
            "timestamp": utc_now().isoformat(),
            "status": "online",
            "hostname": "rewritten",
        },
        headers={"Authorization": f"Bearer {reg_token}"},
    )
    db.expire_all()
    row = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one()
    assert row.hostname != "rewritten", (
        f"{resp.status_code}: shared registration key rewrote the archived agent "
        f"(hostname={row.hostname}, last_seen={row.last_seen})"
    )


async def test_registration_key_cannot_rewrite_an_archived_agent_with_a_key(
    client: AsyncClient, db: AsyncSession
) -> None:
    await _reg_key_heartbeat_onto_archived(client, db, revoke=False)


async def test_registration_key_cannot_rewrite_an_archived_keyless_agent(
    client: AsyncClient, db: AsyncSession
) -> None:
    await _reg_key_heartbeat_onto_archived(client, db, revoke=True)
