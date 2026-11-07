"""A viewer reads a job's error, params and result with every credential
masked, on the admin API and the web detail panel; an editor reads them as
the agent sent them.

An agent writes a job's error and result: a scanned host's name or banner, a
discover warning, a connection error can each carry a credential. Each test
has an editor control, so a page that rendered nothing cannot pass for one
that masked.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from app.models.enum_model import JobStatus, JobType
from app.models.job_model import Job
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.api_token_core_service import ApiTokenCoreService
from app.services.core.auth_core_service import AuthCoreService
from fixtures.factories import make_agent, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

SECRET = "S3cret-Pw"
JOBS = "/api/v1/admin/jobs"

SCAN_RESULT: dict[str, Any] = {
    "subnet": "192.168.7.0/24",
    "hosts_scanned": 254,
    "hosts_responding": 1,
    "scan_duration_seconds": 1.0,
    "discovered_hosts": [
        {
            "ip": "192.168.7.11",
            "hostname": f"token={SECRET}",
            "responds": True,
            "open_ports": [443],
        },
    ],
    "scan_params": {"timeout": 10, "ports": [443], "max_concurrent": 100},
}
ERROR = f"connect to postgresql://monitor:{SECRET}@db.internal/app failed"
PARAMS = {
    "subnet": "192.168.7.0/24",
    "timeout": 5,
    "max_concurrent": 100,
    "ports": [443],
    "api_key": SECRET,
}


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    limiter.reset()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as c:
        yield c
    limiter.reset()


async def _job(db: AsyncSession, **fields: object) -> UUID:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    job = Job(
        id=uuid4(),
        job_type=JobType.NETWORK_SCAN,
        agent_id=agent.id,
        params=PARAMS,
        priority=1,
        created_by="tester",
        **fields,
    )
    db.add(job)
    await db.flush()
    return job.id


async def _scan(db: AsyncSession) -> UUID:
    return await _job(db, status=JobStatus.COMPLETED, result=SCAN_RESULT, error=None)


async def _failed(db: AsyncSession) -> UUID:
    return await _job(db, status=JobStatus.FAILED, result=None, error=ERROR)


async def _token(db: AsyncSession, role: str) -> dict[str, str]:
    user = make_user(role=role)
    db.add(user)
    await db.flush()
    _, plaintext = await ApiTokenCoreService.create_token(
        db, user.id, ApiTokenCreate(name=role, scopes=["*"])
    )
    return {"Authorization": f"Bearer {plaintext}"}


async def _sign_in(client: AsyncClient, db: AsyncSession, role: str) -> None:
    user = make_user(role=role)
    db.add(user)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, user.id)
    client.cookies.set(settings.security.session_cookie_name, token)


@pytest.mark.parametrize("seed", [_scan, _failed])
async def test_the_admin_api_masks_a_job_for_a_viewer(
    client: AsyncClient, db: AsyncSession, seed: Any
) -> None:
    job_id = await seed(db)

    shown = await client.get(f"{JOBS}/{job_id}", headers=await _token(db, "editor"))
    assert shown.status_code == 200
    assert SECRET in shown.text

    viewer = await _token(db, "viewer")
    one = await client.get(f"{JOBS}/{job_id}", headers=viewer)
    page = await client.get(JOBS, headers=viewer)

    assert one.status_code == 200 and page.status_code == 200
    assert one.json()["id"] == str(job_id)
    assert [job["id"] for job in page.json()["items"]] == [str(job_id)]
    assert SECRET not in one.text
    assert SECRET not in page.text


async def test_a_viewer_keeps_the_shape_and_figures_of_a_scan(
    client: AsyncClient, db: AsyncSession
) -> None:
    job_id = await _scan(db)

    body = (
        await client.get(f"{JOBS}/{job_id}", headers=await _token(db, "viewer"))
    ).json()

    host = body["result"]["discovered_hosts"][0]
    assert host["ip"] == "192.168.7.11"
    assert host["open_ports"] == [443]
    assert body["params"]["subnet"] == "192.168.7.0/24"
    assert body["params"]["api_key"] == "***"


@pytest.mark.parametrize("seed", [_scan, _failed])
async def test_the_detail_panel_masks_a_job_for_a_viewer(
    client: AsyncClient, db: AsyncSession, seed: Any
) -> None:
    job_id = await seed(db)

    await _sign_in(client, db, "editor")
    editor = await client.get(f"/jobs/{job_id}/detail")
    assert editor.status_code == 200
    assert SECRET in editor.text

    await _sign_in(client, db, "viewer")
    viewer = await client.get(f"/jobs/{job_id}/detail")

    assert viewer.status_code == 200
    assert "192.168.7.11" in viewer.text or "failed" in viewer.text.lower()
    assert SECRET not in viewer.text
