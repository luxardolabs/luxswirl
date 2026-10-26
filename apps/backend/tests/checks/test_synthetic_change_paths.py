"""Every path that changes a synthetic check needs an admin, and an admin keeps it.

Found by attacking the bulk fix: the single toggle refused admins too, the
dependents form wrote a synthetic check's dependency for an editor, a bulk
modify attached alerts to the synthetic checks it had refused, and an import
job trusted its owner instead of judging them when it ran.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from app.background.maintenance_handlers import (
    bulk_check_import,
    bulk_check_modify,
)
from app.core.config import settings
from app.core.exceptions import AuthorizationException
from app.core.rate_limit import limiter
from app.main import app
from app.models.agent_model import Agent
from app.models.alert_check_mapping_model import AlertCheckMapping
from app.models.check_model import Check
from app.models.enum_model import MaintenanceJobKind
from app.models.maintenance_job_model import MaintenanceJob
from app.schemas.check_schema import CheckUpdate
from app.services.core.agent_assignment_core_service import AgentAssignmentCoreService
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.check_core_service import CheckCoreService
from fixtures.factories import make_agent, make_alert, make_check, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def _browser(db: AsyncSession, role: str):
    user = make_user(role=role)
    db.add(user)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, user.id)
    client = AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    )
    client.cookies.set(settings.security.session_cookie_name, token)
    return client, user.id


@pytest_asyncio.fixture
async def s(db: AsyncSession, monkeypatch) -> AsyncIterator[dict]:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    limiter.reset()
    home = make_agent(tags=["home"])
    other = make_agent(tags=["plain"])
    db.add_all([home, other])
    await db.flush()
    synthetic = make_check(
        agent_id=home.id,
        check_type="synthetic",
        target="https://a.example",
        script_code="await page.goto(target)",
    )
    repl = make_check(
        agent_id=home.id,
        check_type="synthetic",
        target="https://b.example",
        script_code="await page.goto(target)",
        assignment_mode="replicate",
        agent_selector={"tags": ["secure"]},
    )
    ping = make_check(agent_id=home.id)
    db.add_all([synthetic, repl, ping])
    await db.flush()
    editor, editor_id = await _browser(db, "editor")
    admin, _ = await _browser(db, "admin")
    yield {
        "home": home.id,
        "other": other.id,
        "synthetic": synthetic.id,
        "repl": repl.id,
        "ping": ping.id,
        "editor": editor,
        "editor_id": editor_id,
        "admin": admin,
    }
    await editor.aclose()
    await admin.aclose()
    limiter.reset()


async def _fresh(db: AsyncSession, model, pk):
    await db.flush()
    db.expire_all()
    return await db.get(model, pk, populate_existing=True)


# --- 1. single web toggle --------------------------------------------------


async def test_control_admin_single_toggle_of_a_ping_check_works(
    db: AsyncSession, s: dict
) -> None:
    r = await s["admin"].post(f"/checks/{s['ping']}/toggle")
    assert r.status_code == 200, r.text[:300]
    assert (await _fresh(db, Check, s["ping"])).enabled is False


async def test_admin_single_toggle_of_a_synthetic_check_works(
    db: AsyncSession, s: dict
) -> None:
    r = await s["admin"].post(f"/checks/{s['synthetic']}/toggle")
    assert r.status_code == 200, (r.status_code, r.text[:300])
    assert (await _fresh(db, Check, s["synthetic"])).enabled is False


# --- 2. dependents panel ---------------------------------------------------


async def test_control_core_update_refuses_editor_reparenting_a_synthetic(
    db: AsyncSession, s: dict
) -> None:
    with pytest.raises(AuthorizationException):
        await CheckCoreService.update_check(
            db, s["synthetic"], CheckUpdate(depends_on_check_id=s["ping"])
        )


async def test_editor_cannot_reparent_a_synthetic_check_via_dependents(
    db: AsyncSession, s: dict
) -> None:
    await s["editor"].post(
        f"/checks/{s['ping']}/dependents",
        data={"dependent_ids": [str(s["synthetic"])]},
    )
    assert (await _fresh(db, Check, s["synthetic"])).depends_on_check_id is None


# --- 3. agent edits change which agents run a replicate synthetic -----------


async def _runs_on(db: AsyncSession, agent_id, check_id) -> bool:
    agent = await _fresh(db, Agent, agent_id)
    return check_id in {
        c.id for c in await AgentAssignmentCoreService.get_checks_for_agent(db, agent)
    }


async def test_editor_bulk_modify_does_not_attach_alerts_to_refused_synthetic(
    db: AsyncSession, s: dict
) -> None:
    alert = make_alert()
    db.add(alert)
    await db.flush()
    job = MaintenanceJob(
        id=uuid4(),
        kind=MaintenanceJobKind.BULK_CHECK_MODIFY,
        params={
            "check_ids": [str(s["synthetic"]), str(s["ping"])],
            "update_fields": {"interval_seconds": 120},
            "new_agent_id": None,
            "alert_id": str(alert.id),
        },
        owner_id=s["editor_id"],
    )
    await bulk_check_modify.handle(db, job)
    await db.flush()
    rows = (
        (
            await db.execute(
                select(AlertCheckMapping.check_id).where(
                    AlertCheckMapping.alert_id == alert.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert s["ping"] in rows  # control: the editor's own check got it
    assert (await _fresh(db, Check, s["synthetic"])).interval_seconds == 60  # refused
    assert s["synthetic"] not in rows


# --- 6. import job ignores its owner's role at run time ----------------------


async def test_an_import_job_owned_by_a_non_admin_cannot_rewrite_a_synthetic(
    db: AsyncSession, s: dict
) -> None:
    synthetic = await _fresh(db, Check, s["synthetic"])
    name = synthetic.display_name
    job = MaintenanceJob(
        id=uuid4(),
        kind=MaintenanceJobKind.BULK_CHECK_IMPORT,
        params={
            "agent_id": str(s["home"]),
            "mode": "replace",
            "checks": [
                {"name": name, "check_type": "http", "target": "https://x.example"}
            ],
        },
        owner_id=s["editor_id"],  # e.g. an admin demoted after queueing it
    )
    db.add(job)
    await db.flush()
    await bulk_check_import.handle(db, job)
    after = await _fresh(db, Check, s["synthetic"])
    assert after.target == "https://a.example"


# --- 7. regression: synchronous web bulk toggle, mixed batch -----------------


@pytest.mark.parametrize(
    ("who", "synthetic_disabled"), [("admin", True), ("editor", False)]
)
async def test_sync_bulk_disable_mixed_batch(
    db: AsyncSession, s: dict, who: str, synthetic_disabled: bool
) -> None:
    r = await s[who].post(
        "/checks/bulk-action",
        data={
            "action": "disable",
            "select_all": "false",
            "check_ids": [str(s["synthetic"]), str(s["ping"])],
        },
    )
    assert r.status_code == 200, r.text[:300]
    assert (await _fresh(db, Check, s["ping"])).enabled is False
    disabled = (await _fresh(db, Check, s["synthetic"])).enabled is False
    assert disabled is synthetic_disabled
    if who == "editor":
        assert "1 failed" in r.headers["HX-Trigger"]
