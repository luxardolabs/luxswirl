"""Every address a check connects to is validated when the check is saved, and
the agent is told the private-network setting so it enforces the same policy.

A DNS check connects to its nameserver and a database check to the host in its
connection string, not only to `target`. Each is refused at create and update
when it is in a blocked range. The checks list and the heartbeat response carry
`security.block_private_networks`.

Literal IPs resolve to themselves, so nothing here touches the network.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from app.core.check_target_validator import CheckTargetBlockedError
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.rate_limit import limiter
from app.main import app
from app.schemas.check_schema import CheckCreate, CheckUpdate
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.check_core_service import CheckCoreService
from app.services.core.settings_core_service import SettingsCoreService
from fixtures.factories import make_agent
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

METADATA_IP = "169.254.169.254"
PRIVATE_IP = "10.0.0.5"

# check_type, the field that holds the address, a value that puts it at `ip`.
_ENDPOINTS: list[tuple[str, str, Any]] = [
    ("dns", "nameserver", lambda ip: ip),
    ("mysql", "connection_string", lambda ip: f"mysql://u:s3cret@{ip}:3306/db"),
    ("postgres", "connection_string", lambda ip: f"postgresql://u:s3cret@{ip}/db"),
]


def _create(check_type: str, **fields: Any) -> CheckCreate:
    return CheckCreate(
        display_name=f"ssrf-{check_type}",
        check_type=check_type,
        # A documentation address: outside every blocked range, resolves to itself.
        target="192.0.2.10" if check_type != "dns" else "example.com",
        interval_seconds=60,
        timeout_seconds=5,
        **fields,
    )


async def _private_networks(db: AsyncSession, blocked: bool) -> None:
    await SettingsCoreService.ensure_default_settings(db)
    await SettingsCoreService.update_setting(
        db, "security.block_private_networks", blocked
    )


@pytest.mark.parametrize(("check_type", "field", "at"), _ENDPOINTS)
async def test_create_refuses_a_metadata_address_in_any_endpoint(
    db: AsyncSession, check_type: str, field: str, at: Any
) -> None:
    agent = make_agent()
    db.add(agent)
    await db.flush()

    with pytest.raises(CheckTargetBlockedError) as refused:
        await CheckCoreService.create_check(
            db, agent.id, _create(check_type, **{field: at(METADATA_IP)})
        )

    assert "s3cret" not in str(refused.value)


@pytest.mark.parametrize(("check_type", "field", "at"), _ENDPOINTS)
async def test_update_refuses_a_metadata_address_in_any_endpoint(
    db: AsyncSession, check_type: str, field: str, at: Any
) -> None:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = await CheckCoreService.create_check(
        db, agent.id, _create(check_type, **{field: at("192.0.2.10")})
    )

    with pytest.raises(CheckTargetBlockedError):
        await CheckCoreService.update_check(
            db, check.id, CheckUpdate(**{field: at(METADATA_IP)})
        )


@pytest.mark.parametrize(("check_type", "field", "at"), _ENDPOINTS)
async def test_a_private_endpoint_follows_the_private_network_setting(
    db: AsyncSession, check_type: str, field: str, at: Any
) -> None:
    agent = make_agent()
    db.add(agent)
    await db.flush()

    await _private_networks(db, False)
    allowed = await CheckCoreService.create_check(
        db, agent.id, _create(check_type, **{field: at(PRIVATE_IP)})
    )
    assert allowed.id is not None

    await _private_networks(db, True)
    with pytest.raises(CheckTargetBlockedError):
        await CheckCoreService.create_check(
            db, agent.id, _create(check_type, **{field: at(PRIVATE_IP)})
        )


async def test_clone_refuses_a_nameserver_that_is_now_blocked(
    db: AsyncSession,
) -> None:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    await _private_networks(db, False)
    source = await CheckCoreService.create_check(
        db, agent.id, _create("dns", nameserver=PRIVATE_IP)
    )

    await _private_networks(db, True)
    with pytest.raises(CheckTargetBlockedError):
        await CheckCoreService.clone_check(db, source.id, agent.id)


@pytest.mark.parametrize(
    "dsn",
    [
        f"postgresql://u:p@/db?host={METADATA_IP}",
        f"postgresql://u:p@/db?hostaddr={METADATA_IP}",
        f"postgresql://u:p@192.0.2.10,{METADATA_IP}:5432/db",
        f"postgresql://u:p@[::ffff:{METADATA_IP}]/db",
    ],
    ids=["host-param", "hostaddr-param", "multi-host", "mapped-v6"],
)
async def test_a_postgres_url_cannot_hide_a_metadata_host(
    db: AsyncSession, dsn: str
) -> None:
    """libpq takes hosts from a comma list and from `host`/`hostaddr`; each is checked."""
    agent = make_agent()
    db.add(agent)
    await db.flush()

    with pytest.raises(CheckTargetBlockedError):
        await CheckCoreService.create_check(
            db, agent.id, _create("postgres", connection_string=dsn)
        )


@pytest.mark.parametrize(
    ("check_type", "fields"),
    [
        ("http", {"target": f"http://[::ffff:{METADATA_IP}]/latest/"}),
        ("tcp", {"target": "::ffff:a9fe:a9fe"}),
        ("dns", {"nameserver": f"::ffff:{METADATA_IP}"}),
        ("tcp", {"target": "64:ff9b::a9fe:a9fe"}),
        ("tcp", {"target": "2002:a9fe:a9fe::1"}),
    ],
    ids=["http-mapped", "tcp-mapped-hex", "dns-mapped", "tcp-nat64", "tcp-6to4"],
)
async def test_an_ipv6_form_of_the_metadata_address_is_refused(
    db: AsyncSession, check_type: str, fields: dict[str, Any]
) -> None:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    data = _create(check_type).model_copy(update=fields)

    with pytest.raises(CheckTargetBlockedError):
        await CheckCoreService.create_check(db, agent.id, data)


# ---------------------------------------------------------------------------
# The agent is told the setting.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(db: AsyncSession, monkeypatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    limiter.reset()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            yield c
    finally:
        limiter.reset()


@pytest_asyncio.fixture
async def approved(db: AsyncSession) -> tuple[str, str]:
    agent = make_agent(approval_status="pending")
    db.add(agent)
    await db.flush()
    _, key = await AgentCoreService.approve_agent(db, agent.id)
    return str(agent.id), key


@pytest.mark.parametrize("blocked", [True, False])
async def test_the_checks_list_carries_the_private_network_setting(
    client: AsyncClient, db: AsyncSession, approved: tuple[str, str], blocked: bool
) -> None:
    agent_id, key = approved
    await _private_networks(db, blocked)

    resp = await client.get(
        "/api/v1/checks",
        params={"agent_id": agent_id},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["block_private_networks"] is blocked


@pytest.mark.parametrize("blocked", [True, False])
async def test_the_heartbeat_response_carries_the_private_network_setting(
    client: AsyncClient, db: AsyncSession, approved: tuple[str, str], blocked: bool
) -> None:
    agent_id, key = approved
    await _private_networks(db, blocked)

    resp = await client.post(
        "/api/v1/heartbeat",
        json={
            "agent_id": agent_id,
            "timestamp": utc_now().isoformat(),
            "status": "online",
        },
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["block_private_networks"] is blocked
