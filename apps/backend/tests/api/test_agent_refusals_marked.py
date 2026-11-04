"""Every refusal the server sends an agent carries the refusal header.

The agent believes a 401 or 403 only when it is marked (`X-LuxSwirl-Refused`):
an unmarked one is a proxy's or a gateway's, and the agent retries it as
temporary. So every agent-protocol route must mark every refusal it sends.
Each route of `/api/v1/agent` is an `AgentProtocolRoute` (a new route on any
other route class fails here), and each one, probed with credentials it
refuses, answers marked; the probes must cover every route there is.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from _route_table import iter_routes
from app.core.agent_refusals import AgentProtocolRoute
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.rate_limit import limiter
from app.main import app
from app.schemas.job_schema import JobCreate
from app.schemas.registration_key_schema import RegistrationKeyCreate
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.job_core_service import JobCoreService
from app.services.core.registration_key_core_service import RegistrationKeyCoreService
from app.shared.agent_protocol import (
    AGENT_KEY_REFUSED,
    RECOVERY_SECRET_REFUSED,
    REFUSAL_HEADER,
    REFUSED,
    REGISTRATION_KEY_REFUSED,
)
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

AGENT_PREFIX = "/api/v1/agent"
WRONG_KEY = {"Authorization": "Bearer luxswirl_ak_wrong"}


def _agent_route_table() -> list[tuple[object, str]]:
    """Every leaf route the app serves under the agent protocol, with its
    full path, whatever kind of route it is."""
    return [
        (route, path)
        for route, path in iter_routes(app.routes)
        if path.startswith(AGENT_PREFIX)
    ]


def _agent_routes() -> set[tuple[str, str]]:
    """Every (method, path) the app serves under the agent protocol."""
    return {
        (method, path)
        for route, path in _agent_route_table()
        for method in getattr(route, "methods", None) or ()
    }


def test_every_agent_route_marks_its_refusals() -> None:
    routes = _agent_route_table()
    assert len(routes) >= 8
    unmarked = [
        path for route, path in routes if not isinstance(route, AgentProtocolRoute)
    ]
    assert unmarked == [], f"agent routes that would send unmarked refusals: {unmarked}"


@pytest_asyncio.fixture
async def client(db: AsyncSession, monkeypatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    limiter.reset()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _registered_and_approved(
    client: AsyncClient, db: AsyncSession
) -> tuple[UUID, str, str, str]:
    """A registration key, and an agent registered with it then approved:
    its id, its recovery secret and its own key."""
    _, registration_key = await RegistrationKeyCoreService.create_key(
        db, RegistrationKeyCreate(name="refusal-marks"), created_by="tests"
    )
    resp = await client.post(
        f"{AGENT_PREFIX}/register",
        json={"hostname": "refused.test"},
        headers={"Authorization": f"Bearer {registration_key}"},
    )
    assert resp.status_code == 201, resp.text
    agent_id = UUID(resp.json()["agent_id"])
    _, key = await AgentCoreService.approve_agent(db, agent_id, actor_is_admin=True)
    return agent_id, resp.json()["recovery_secret"], key, registration_key


def _beat(agent_id: UUID) -> dict[str, object]:
    return {
        "agent_id": str(agent_id),
        "timestamp": utc_now().isoformat(),
        "status": "online",
    }


async def test_each_agent_route_marks_a_refusal(
    client: AsyncClient, db: AsyncSession
) -> None:
    agent_id, _secret, _key, _ = await _registered_and_approved(client, db)
    job = await JobCoreService.create_job(
        db,
        JobCreate(
            job_type="network_scan",
            agent_id=agent_id,
            params={"subnet": "10.0.0.0/30"},
        ),
    )
    artifact = {
        "agent_id": str(agent_id),
        "check_id": str(uuid4()),
        "check_result_id": str(uuid4()),
        "check_result_timestamp": utc_now().isoformat(),
        "artifact_type": "screenshot",
        "content_type": "image/png",
        "filename": "shot.png",
        "data_base64": base64.b64encode(b"png").decode(),
    }
    probes: dict[tuple[str, str], Response] = {}

    async def probe(method: str, template: str, path: str, **kwargs) -> None:
        limiter.reset()
        probes[(method, template)] = await client.request(method, path, **kwargs)

    await probe(
        "POST",
        f"{AGENT_PREFIX}/register",
        f"{AGENT_PREFIX}/register",
        json={"hostname": "x"},
        headers={"Authorization": "Bearer luxswirl_rk_wrong"},
    )
    await probe(
        "POST",
        f"{AGENT_PREFIX}/heartbeat",
        f"{AGENT_PREFIX}/heartbeat",
        json=_beat(agent_id),
        headers=WRONG_KEY,
    )
    await probe(
        "POST",
        f"{AGENT_PREFIX}/{{agent_id}}/recover-key",
        f"{AGENT_PREFIX}/{agent_id}/recover-key",
        headers={"Authorization": "Bearer luxswirl_rk_wrong"},
    )
    await probe(
        "GET",
        f"{AGENT_PREFIX}/checks",
        f"{AGENT_PREFIX}/checks",
        params={"agent_id": str(agent_id)},
        headers=WRONG_KEY,
    )
    await probe(
        "POST",
        f"{AGENT_PREFIX}/reports",
        f"{AGENT_PREFIX}/reports",
        json={"agent_id": str(agent_id), "checks": []},
        headers=WRONG_KEY,
    )
    await probe(
        "POST",
        f"{AGENT_PREFIX}/jobs/{{job_id}}/start",
        f"{AGENT_PREFIX}/jobs/{job.id}/start",
        headers=WRONG_KEY,
    )
    await probe(
        "POST",
        f"{AGENT_PREFIX}/jobs/{{job_id}}/results",
        f"{AGENT_PREFIX}/jobs/{job.id}/results",
        json={"status": "completed"},
        headers=WRONG_KEY,
    )
    await probe(
        "POST",
        f"{AGENT_PREFIX}/artifacts",
        f"{AGENT_PREFIX}/artifacts",
        json=artifact,
        headers=WRONG_KEY,
    )

    # A new agent route needs a probe here too.
    assert set(probes) == _agent_routes()
    for route, resp in probes.items():
        assert resp.status_code in (401, 403), (route, resp.status_code, resp.text)
        assert resp.headers.get(REFUSAL_HEADER), f"{route} sent an unmarked refusal"


async def test_a_refusal_names_what_it_refused(
    client: AsyncClient, db: AsyncSession
) -> None:
    agent_id, secret, _key, registration_key = await _registered_and_approved(
        client, db
    )
    limiter.reset()
    await AgentCoreService.regenerate_agent_key(db, agent_id)
    key_alone = await client.post(
        f"{AGENT_PREFIX}/heartbeat", json=_beat(agent_id), headers=WRONG_KEY
    )
    wrong_secret = await client.post(
        f"{AGENT_PREFIX}/heartbeat",
        json=_beat(agent_id),
        headers={**WRONG_KEY, "X-Agent-Recovery-Secret": "luxswirl_rs_wrong"},
    )
    limiter.reset()
    recover_wrong_secret = await client.post(
        f"{AGENT_PREFIX}/{agent_id}/recover-key",
        headers={
            "Authorization": f"Bearer {registration_key}",
            "X-Agent-Recovery-Secret": "luxswirl_rs_wrong",
        },
    )
    await AgentCoreService.disable_agent(db, agent_id, reason="tests")
    limiter.reset()
    recover_disabled = await client.post(
        f"{AGENT_PREFIX}/{agent_id}/recover-key",
        headers={
            "Authorization": f"Bearer {registration_key}",
            "X-Agent-Recovery-Secret": secret,
        },
    )

    assert key_alone.status_code == 401
    assert key_alone.headers[REFUSAL_HEADER] == AGENT_KEY_REFUSED
    assert wrong_secret.status_code == 401
    assert wrong_secret.headers[REFUSAL_HEADER] == RECOVERY_SECRET_REFUSED
    assert recover_wrong_secret.status_code == 401
    assert recover_wrong_secret.headers[REFUSAL_HEADER] == RECOVERY_SECRET_REFUSED
    assert recover_disabled.status_code == 403
    assert recover_disabled.headers[REFUSAL_HEADER] == REFUSED


# P2: a pending agent with neither a key nor a secret (an agent build from
# before secrets) whose registration key is refused was marked as refusing
# an agent key it never had.
async def test_a_keyless_agents_refused_registration_key_is_named(
    client: AsyncClient, db: AsyncSession
) -> None:
    _, registration_key = await RegistrationKeyCoreService.create_key(
        db, RegistrationKeyCreate(name="keyless"), created_by="tests"
    )
    resp = await client.post(
        f"{AGENT_PREFIX}/register",
        json={"hostname": "keyless.test"},
        headers={"Authorization": f"Bearer {registration_key}"},
    )
    agent_id = UUID(resp.json()["agent_id"])
    limiter.reset()

    refused = await client.post(
        f"{AGENT_PREFIX}/heartbeat",
        json=_beat(agent_id),
        headers={"Authorization": "Bearer luxswirl_rk_wrong"},
    )

    assert refused.status_code == 401, refused.text
    assert refused.headers[REFUSAL_HEADER] == REGISTRATION_KEY_REFUSED


async def test_a_returned_refusal_is_marked_and_a_set_mark_is_kept() -> None:
    router = APIRouter(route_class=AgentProtocolRoute)

    @router.get("/returned")
    async def returned() -> JSONResponse:
        return JSONResponse({"detail": "no"}, status_code=403)

    @router.get("/raised")
    async def raised() -> None:
        raise HTTPException(401, "no", headers={REFUSAL_HEADER: AGENT_KEY_REFUSED})

    @router.get("/fine")
    async def fine() -> dict[str, str]:
        return {"ok": "yes"}

    probe_app = FastAPI()
    probe_app.include_router(router)
    async with AsyncClient(
        transport=ASGITransport(app=probe_app), base_url="http://test"
    ) as c:
        returned_resp = await c.get("/returned")
        raised_resp = await c.get("/raised")
        fine_resp = await c.get("/fine")

    assert returned_resp.headers[REFUSAL_HEADER] == REFUSED
    assert raised_resp.headers[REFUSAL_HEADER] == AGENT_KEY_REFUSED
    assert REFUSAL_HEADER not in fine_resp.headers
