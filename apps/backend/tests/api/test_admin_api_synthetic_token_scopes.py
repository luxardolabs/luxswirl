"""A token acts as an admin for synthetic checks only when it grants `checks:admin`.

A synthetic check runs its script on an agent, so everything only an admin may
do to one (create, change, clone, enable or disable, delete, select in bulk,
make a dependent, attach to an alert, move or delete with its agent) needs an
admin user AND a token whose scopes grant `checks:admin`. An admin's "Read and
write" token (`*:read`, `*:write`) is refused each of them, and nothing is
written; a full (`*`) token, or one that names `checks:admin`, is not.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from app.models.alert_check_mapping_model import AlertCheckMapping
from app.models.check_model import Check
from app.models.maintenance_job_model import MaintenanceJob
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.api_token_core_service import ApiTokenCoreService
from fixtures.factories import make_agent, make_alert, make_check, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

ADMIN = "/api/v1/admin"
CHECKS = f"{ADMIN}/checks"
READ_WRITE = ["*:read", "*:write"]
SCRIPT = (
    "async def run_check(page):\n"
    "    await page.goto('https://a.example')\n"
    "    return {'status': 'success', 'steps': ['Loaded page']}\n"
)
REFUSED = "Synthetic checks require admin privileges"


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    limiter.reset()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as c:
        yield c
    limiter.reset()


async def _admin_token(db: AsyncSession, scopes: list[str]) -> dict[str, str]:
    user = make_user(role="admin")
    db.add(user)
    await db.flush()
    _, plaintext = await ApiTokenCoreService.create_token(
        db, user.id, ApiTokenCreate(name="admin", scopes=scopes)
    )
    return {"Authorization": f"Bearer {plaintext}"}


async def _agent(db: AsyncSession) -> UUID:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    return agent.id


async def _synthetic(db: AsyncSession, agent_id: UUID) -> UUID:
    check = make_check(
        agent_id=agent_id,
        check_type="synthetic",
        target="https://a.example",
        script_code=SCRIPT,
    )
    db.add(check)
    await db.flush()
    return check.id


async def _plain(db: AsyncSession, agent_id: UUID) -> UUID:
    check = make_check(agent_id=agent_id, check_type="ping", target="192.0.2.1")
    db.add(check)
    await db.flush()
    return check.id


def _synthetic_body(agent_id: UUID) -> dict[str, str]:
    return {
        "agent_id": str(agent_id),
        "display_name": "browser-flow",
        "check_type": "synthetic",
        "target": "https://a.example",
        "script_code": SCRIPT,
    }


async def _checks_on(db: AsyncSession, agent_id: UUID) -> int:
    db.expire_all()
    return (
        await db.scalar(
            select(func.count()).select_from(Check).where(Check.agent_id == agent_id)
        )
        or 0
    )


def _refused(response) -> None:
    assert response.status_code == 403, response.text
    assert REFUSED in response.text


# ── An admin's read-and-write token ──────────────────────────────────────────


async def test_a_read_write_admin_token_cannot_create_a_synthetic_check(
    db: AsyncSession, client: AsyncClient
) -> None:
    token = await _admin_token(db, READ_WRITE)
    agent_id = await _agent(db)

    _refused(await client.post(CHECKS, json=_synthetic_body(agent_id), headers=token))
    assert await _checks_on(db, agent_id) == 0


async def test_a_read_write_admin_token_cannot_rewrite_a_synthetic_script(
    db: AsyncSession, client: AsyncClient
) -> None:
    token = await _admin_token(db, READ_WRITE)
    check_id = await _synthetic(db, await _agent(db))

    response = await client.patch(
        f"{CHECKS}/{check_id}",
        json={"script_code": SCRIPT.replace("a.example", "b.example")},
        headers=token,
    )

    _refused(response)
    db.expire_all()
    assert await db.scalar(select(Check.script_code).where(Check.id == check_id)) == (
        SCRIPT
    )


async def test_a_read_write_admin_token_cannot_turn_a_check_synthetic(
    db: AsyncSession, client: AsyncClient
) -> None:
    token = await _admin_token(db, READ_WRITE)
    check_id = await _plain(db, await _agent(db))

    response = await client.patch(
        f"{CHECKS}/{check_id}",
        json={
            "check_type": "synthetic",
            "target": "https://a.example",
            "script_code": SCRIPT,
        },
        headers=token,
    )

    _refused(response)
    db.expire_all()
    assert await db.scalar(select(Check.check_type).where(Check.id == check_id)) == (
        "ping"
    )


async def test_a_read_write_admin_token_cannot_clone_toggle_or_delete_one(
    db: AsyncSession, client: AsyncClient
) -> None:
    token = await _admin_token(db, READ_WRITE)
    agent_id = await _agent(db)
    check_id = await _synthetic(db, agent_id)

    _refused(await client.post(f"{CHECKS}/{check_id}/clone", json={}, headers=token))
    _refused(await client.post(f"{CHECKS}/{check_id}/disable", headers=token))
    _refused(await client.delete(f"{CHECKS}/{check_id}", headers=token))

    assert await _checks_on(db, agent_id) == 1
    assert await db.scalar(select(Check.enabled).where(Check.id == check_id)) is True


async def test_a_read_write_admin_token_cannot_select_one_in_bulk(
    db: AsyncSession, client: AsyncClient
) -> None:
    token = await _admin_token(db, READ_WRITE)
    agent_id = await _agent(db)
    plain = await _plain(db, agent_id)
    synthetic = await _synthetic(db, agent_id)
    ids = {"check_ids": [str(plain), str(synthetic)]}

    _refused(await client.post(f"{CHECKS}/bulk/disable", json=ids, headers=token))
    _refused(await client.post(f"{CHECKS}/bulk/delete", json=ids, headers=token))
    _refused(
        await client.post(
            f"{CHECKS}/bulk/modify", json={**ids, "interval_seconds": 30}, headers=token
        )
    )

    db.expire_all()
    enabled = (
        await db.scalars(select(Check.enabled).where(Check.id.in_([plain, synthetic])))
    ).all()
    assert enabled == [True, True]
    assert await db.scalar(select(func.count()).select_from(MaintenanceJob)) == 0


async def test_a_read_write_admin_token_cannot_make_one_a_dependent(
    db: AsyncSession, client: AsyncClient
) -> None:
    token = await _admin_token(db, READ_WRITE)
    agent_id = await _agent(db)
    parent = await _plain(db, agent_id)
    synthetic = await _synthetic(db, agent_id)

    response = await client.put(
        f"{CHECKS}/{parent}/dependents",
        json={"dependent_ids": [str(synthetic)]},
        headers=token,
    )

    _refused(response)
    db.expire_all()
    assert (
        await db.scalar(select(Check.depends_on_check_id).where(Check.id == synthetic))
        is None
    )


async def test_a_read_write_admin_token_cannot_attach_one_to_an_alert(
    db: AsyncSession, client: AsyncClient
) -> None:
    token = await _admin_token(db, READ_WRITE)
    synthetic = await _synthetic(db, await _agent(db))
    alert = make_alert()
    db.add(alert)
    await db.flush()
    alert_id = alert.id

    response = await client.put(
        f"{ADMIN}/alerts/{alert_id}/checks",
        json={"check_ids": [str(synthetic)]},
        headers=token,
    )

    _refused(response)
    db.expire_all()
    mapped = (
        await db.scalars(
            select(AlertCheckMapping.check_id).where(
                AlertCheckMapping.alert_id == alert_id
            )
        )
    ).all()
    assert mapped == []


async def test_a_read_write_admin_token_cannot_delete_an_agent_that_owns_one(
    db: AsyncSession, client: AsyncClient
) -> None:
    token = await _admin_token(db, READ_WRITE)
    agent_id = await _agent(db)
    await _synthetic(db, agent_id)

    _refused(await client.delete(f"{ADMIN}/agents/{agent_id}", headers=token))
    assert await db.scalar(select(func.count()).select_from(MaintenanceJob)) == 0


# ── Controls: the admin scope, by wildcard or by name ────────────────────────


@pytest.mark.parametrize(
    "scopes",
    [["*"], ["*:read", "*:write", "checks:admin"], ["checks:*"]],
    ids=["full", "read-write-and-checks-admin", "checks-family"],
)
async def test_an_admin_token_granting_checks_admin_writes_synthetic_checks(
    db: AsyncSession, client: AsyncClient, scopes: list[str]
) -> None:
    token = await _admin_token(db, scopes)
    agent_id = await _agent(db)

    created = await client.post(CHECKS, json=_synthetic_body(agent_id), headers=token)
    assert created.status_code == 201, created.text
    check_id = created.json()["id"]

    changed = await client.patch(
        f"{CHECKS}/{check_id}",
        json={"script_code": SCRIPT.replace("a.example", "b.example")},
        headers=token,
    )
    assert changed.status_code == 200, changed.text

    disabled = await client.post(
        f"{CHECKS}/bulk/disable", json={"check_ids": [check_id]}, headers=token
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["success_count"] == 1


async def test_a_read_write_admin_token_still_writes_other_checks(
    db: AsyncSession, client: AsyncClient
) -> None:
    token = await _admin_token(db, READ_WRITE)
    agent_id = await _agent(db)
    check_id = await _plain(db, agent_id)

    response = await client.patch(
        f"{CHECKS}/{check_id}", json={"interval_seconds": 30}, headers=token
    )

    assert response.status_code == 200, response.text
