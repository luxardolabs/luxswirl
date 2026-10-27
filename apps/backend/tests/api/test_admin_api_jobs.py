"""Agent jobs on the admin API: any token reads, an editor changes.

A job is held to its type's params and runs on an agent; a scan's hosts
become checks, one kind per request, skipping those that exist.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from app.models.check_model import Check
from app.models.enum_model import JobStatus, JobType
from app.models.job_model import Job
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.api_token_core_service import ApiTokenCoreService
from fixtures.factories import make_agent, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

JOBS = "/api/v1/admin/jobs"

SCAN_RESULT = {
    "discovered_hosts": [
        {"ip": "192.168.7.10", "hostname": None, "open_ports": [22, 80, 5432]},
        {"ip": "192.168.7.11", "hostname": "nas", "open_ports": [443]},
    ],
    "scan_params": {"timeout": 10, "ports": [22, 80, 443, 5432], "max_concurrent": 100},
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


async def _user(db: AsyncSession, role: str) -> tuple[str, dict[str, str]]:
    user = make_user(role=role)
    db.add(user)
    await db.flush()
    _, plaintext = await ApiTokenCoreService.create_token(
        db, user.id, ApiTokenCreate(name=role, scopes=["*"])
    )
    return user.username, {"Authorization": f"Bearer {plaintext}"}


async def _as(db: AsyncSession, role: str) -> dict[str, str]:
    return (await _user(db, role))[1]


async def _agent(db: AsyncSession, **overrides: object) -> UUID:
    agent = make_agent(**overrides)
    db.add(agent)
    await db.flush()
    return agent.id


async def _job(db: AsyncSession, **fields: object) -> UUID:
    values: dict = {
        "job_type": JobType.NETWORK_SCAN,
        "agent_id": await _agent(db),
        "params": {"subnet": "192.168.7.0/24"},
        "status": JobStatus.PENDING,
        **fields,
    }
    job = Job(**values)
    db.add(job)
    await db.flush()
    return job.id


async def _checks(db: AsyncSession, agent_id: UUID) -> list[Check]:
    db.expire_all()
    return list(
        (await db.scalars(select(Check).where(Check.agent_id == agent_id))).all()
    )


# ── Who may do what ─────────────────────────────────────────────────────────


async def test_no_token_is_refused(client: AsyncClient) -> None:
    assert (await client.get(JOBS)).status_code == 401


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "", {"job_type": "network_discover"}),
        ("post", "/{id}/cancel", None),
        ("delete", "/{id}", None),
        ("post", "/{id}/checks", {"kind": "ping", "agent_id": str(uuid4())}),
    ],
)
async def test_a_viewer_reads_but_changes_nothing(
    db: AsyncSession,
    client: AsyncClient,
    method: str,
    path: str,
    body: dict | None,
) -> None:
    viewer = await _as(db, "viewer")
    job_id = await _job(db, result=SCAN_RESULT, status=JobStatus.COMPLETED)

    assert (await client.get(JOBS, headers=viewer)).status_code == 200
    assert (await client.get(f"{JOBS}/{job_id}", headers=viewer)).status_code == 200
    assert (await client.get(f"{JOBS}/types", headers=viewer)).status_code == 200

    kwargs: dict = {"headers": viewer}
    if body is not None:
        kwargs["json"] = body
    resp = await client.request(method, JOBS + path.format(id=job_id), **kwargs)
    assert resp.status_code == 403, resp.text
    assert await db.get(Job, job_id) is not None
    db.expire_all()
    assert (await db.get(Job, job_id)).status == JobStatus.COMPLETED


# ── Types ───────────────────────────────────────────────────────────────────


async def test_types_describe_each_job_and_its_params(
    db: AsyncSession, client: AsyncClient
) -> None:
    resp = await client.get(f"{JOBS}/types", headers=await _as(db, "viewer"))

    assert resp.status_code == 200, resp.text
    by_type = {t["job_type"]: t for t in resp.json()}
    assert set(by_type) == {"network_scan", "network_discover"}
    assert all(t["requires_agent"] for t in by_type.values())
    assert by_type["network_scan"]["params_schema"]["required"] == ["subnet"]
    assert by_type["network_scan"]["timeout_seconds"] == 600


# ── Create ──────────────────────────────────────────────────────────────────


async def test_an_editor_creates_a_scan_held_to_its_params(
    db: AsyncSession, client: AsyncClient
) -> None:
    username, editor = await _user(db, "editor")
    agent_id = await _agent(db, hostname="lab-01")

    resp = await client.post(
        JOBS,
        json={
            "job_type": "network_scan",
            "agent_id": str(agent_id),
            "params": {"subnet": "10.1.0.0/24", "ports": [22, 22]},
            "priority": 5,
            "tags": ["lab"],
        },
        headers=editor,
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["created_by"] == username
    assert body["agent_hostname"] == "lab-01"
    assert body["params"] == {
        "subnet": "10.1.0.0/24",
        "timeout": 10,
        "ports": [22],
        "max_concurrent": 100,
    }
    stored = await db.get(Job, UUID(body["id"]))
    assert stored is not None and stored.params == body["params"]


@pytest.mark.parametrize(
    ("body", "status_code", "says"),
    [
        (
            {"job_type": "network_scan", "params": {"subnet": "10.0.0.0/24"}},
            422,
            "agent",
        ),
        ({"job_type": "network_scan", "agent_id": "{agent}"}, 422, "subnet"),
        (
            {
                "job_type": "network_scan",
                "agent_id": "{agent}",
                "params": {"subnet": "169.254.0.0/16"},
            },
            422,
            "cloud metadata",
        ),
        (
            {
                "job_type": "network_scan",
                "agent_id": "{agent}",
                "params": {"subnet": "10.0.0.0/8"},
            },
            422,
            "too large",
        ),
        ({"job_type": "network_discover", "agent_id": str(uuid4())}, 404, "Agent"),
        ({"job_type": "shell", "agent_id": "{agent}"}, 422, "job_type"),
        (
            {
                "job_type": "network_scan",
                "agent_id": "{agent}",
                "params": {"subnet": "10.0.0.0/24", "port": [22]},
            },
            422,
            "port: Extra inputs",
        ),
        (
            {
                "job_type": "network_scan",
                "agent_id": "{agent}",
                "params": {"subnet": "10.0.0.0/24", "ports": list(range(1, 1100))},
            },
            422,
            "ports",
        ),
        ({"job_type": "network_discover", "agent_id": "{pending}"}, 422, "pending"),
        ({"job_type": "network_discover", "agent_id": "{rejected}"}, 422, "rejected"),
        ({"job_type": "network_discover", "agent_id": "{archived}"}, 422, "archived"),
        (
            {"job_type": "network_discover", "agent_id": "{agent}", "created_by": "x"},
            422,
            "created_by",
        ),
        (
            {"job_type": "network_discover", "agent_id": "{agent}", "priority": 101},
            422,
            "priority",
        ),
    ],
)
async def test_a_job_that_cannot_run_is_refused_and_nothing_queued(
    db: AsyncSession,
    client: AsyncClient,
    body: dict,
    status_code: int,
    says: str,
) -> None:
    editor = await _as(db, "editor")
    agents = {
        "{agent}": await _agent(db),
        "{pending}": await _agent(db, approval_status="pending"),
        "{rejected}": await _agent(db, approval_status="rejected"),
        "{archived}": await _agent(db, approval_status="archived"),
    }
    if body.get("agent_id") in agents:
        body = {**body, "agent_id": str(agents[body["agent_id"]])}
    before = await db.scalar(select(func.count()).select_from(Job))

    resp = await client.post(JOBS, json=body, headers=editor)

    assert resp.status_code == status_code, resp.text
    assert says in resp.text
    assert await db.scalar(select(func.count()).select_from(Job)) == before


# ── Read ────────────────────────────────────────────────────────────────────


async def test_the_list_filters_and_pages(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _as(db, "viewer")
    agent_id = await _agent(db)
    done = await _job(db, agent_id=agent_id, status=JobStatus.COMPLETED)
    await _job(db, agent_id=agent_id, job_type=JobType.NETWORK_DISCOVER, params={})
    await _job(db)

    resp = await client.get(
        JOBS,
        params={"agent_id": str(agent_id), "status": "completed"},
        headers=viewer,
    )
    assert resp.status_code == 200, resp.text
    assert [j["id"] for j in resp.json()["items"]] == [str(done)]

    resp = await client.get(
        JOBS,
        params={"agent_id": str(agent_id), "per_page": 1},
        headers=viewer,
    )
    page = resp.json()
    assert page["total"] == 2 and page["total_pages"] == 2 and page["has_next"]
    assert len(page["items"]) == 1

    resp = await client.get(
        JOBS,
        params={"agent_id": str(agent_id), "job_type": "network_discover"},
        headers=viewer,
    )
    assert [j["job_type"] for j in resp.json()["items"]] == ["network_discover"]


async def test_an_unknown_job_is_not_found(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    missing = uuid4()

    for method, path, body in [
        ("get", "", None),
        ("post", "/cancel", None),
        ("delete", "", None),
        ("post", "/checks", {"kind": "ping", "agent_id": str(await _agent(db))}),
    ]:
        kwargs: dict = {"headers": editor}
        if body is not None:
            kwargs["json"] = body
        resp = await client.request(method, f"{JOBS}/{missing}{path}", **kwargs)
        assert resp.status_code == 404, (method, path, resp.text)
        assert resp.json()["error"] == "NOT_FOUND"


# ── Cancel and delete ───────────────────────────────────────────────────────


async def test_cancel_stops_an_unfinished_job_and_refuses_a_finished_one(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    running = await _job(db, status=JobStatus.RUNNING)
    done = await _job(db, status=JobStatus.COMPLETED, result=SCAN_RESULT)

    resp = await client.post(f"{JOBS}/{running}/cancel", headers=editor)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"

    resp = await client.post(f"{JOBS}/{done}/cancel", headers=editor)
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"] == "JOB_FINISHED"
    db.expire_all()
    assert (await db.get(Job, done)).status == JobStatus.COMPLETED


async def test_delete_removes_the_job(db: AsyncSession, client: AsyncClient) -> None:
    editor = await _as(db, "editor")
    job_id = await _job(db)

    resp = await client.delete(f"{JOBS}/{job_id}", headers=editor)

    assert resp.status_code == 204, resp.text
    db.expire_all()
    assert await db.get(Job, job_id) is None


# ── Checks from a scan ──────────────────────────────────────────────────────


async def test_a_scan_s_hosts_become_checks_once(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    job_id = await _job(db, status=JobStatus.COMPLETED, result=SCAN_RESULT)
    target = await _agent(db)
    body = {
        "kind": "web",
        "agent_id": str(target),
        "retry_attempts": 0,
        "tags": ["scan"],
    }

    resp = await client.post(f"{JOBS}/{job_id}/checks", json=body, headers=editor)

    assert resp.status_code == 201, resp.text
    assert resp.json() == {"created": 2, "skipped": 0}
    checks = {c.target: c for c in await _checks(db, target)}
    assert set(checks) == {"http://192.168.7.10:80", "https://nas:443"}
    assert all(c.retry_attempts == 0 for c in checks.values())
    assert all(c.tags == ["scan"] for c in checks.values())

    again = await client.post(f"{JOBS}/{job_id}/checks", json=body, headers=editor)
    assert again.json() == {"created": 0, "skipped": 2}
    assert len(await _checks(db, target)) == 2


@pytest.mark.parametrize(
    ("kind", "targets"),
    [
        ("ping", {"192.168.7.10", "nas"}),
        ("ssh", {"192.168.7.10:22"}),
        ("database", {"192.168.7.10:5432"}),
    ],
)
async def test_each_kind_takes_its_own_hosts(
    db: AsyncSession, client: AsyncClient, kind: str, targets: set[str]
) -> None:
    editor = await _as(db, "editor")
    job_id = await _job(db, status=JobStatus.COMPLETED, result=SCAN_RESULT)
    target = await _agent(db)

    resp = await client.post(
        f"{JOBS}/{job_id}/checks",
        json={"kind": kind, "agent_id": str(target)},
        headers=editor,
    )

    assert resp.status_code == 201, resp.text
    assert {c.target for c in await _checks(db, target)} == targets


async def test_a_job_with_no_scan_result_creates_nothing(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    job_id = await _job(db)
    target = await _agent(db)

    resp = await client.post(
        f"{JOBS}/{job_id}/checks",
        json={"kind": "ping", "agent_id": str(target)},
        headers=editor,
    )

    assert resp.status_code == 422, resp.text
    assert await _checks(db, target) == []


async def test_checks_go_only_to_an_admitted_agent(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    job_id = await _job(db, status=JobStatus.COMPLETED, result=SCAN_RESULT)
    pending = await _agent(db, approval_status="pending")

    resp = await client.post(
        f"{JOBS}/{job_id}/checks",
        json={"kind": "ping", "agent_id": str(pending)},
        headers=editor,
    )

    assert resp.status_code == 422, resp.text
    assert await _checks(db, pending) == []


# ── What the agent wrote ────────────────────────────────────────────────────


async def test_a_result_s_duration_that_is_not_a_number_is_not_read(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _as(db, "viewer")
    agent_id = await _agent(db)
    job_id = await _job(
        db,
        agent_id=agent_id,
        status=JobStatus.COMPLETED,
        result={**SCAN_RESULT, "scan_duration_seconds": "n/a"},
    )

    listed = await client.get(JOBS, params={"agent_id": str(agent_id)}, headers=viewer)
    one = await client.get(f"{JOBS}/{job_id}", headers=viewer)

    assert listed.status_code == 200, listed.text
    assert one.status_code == 200, one.text
    assert one.json()["duration_seconds"] is None


@pytest.mark.parametrize(
    ("kind", "hosts"),
    [
        ("ping", [{"ip": "192.168.7.10"}, {"ip": "192.168.7.10"}]),
        (
            "ping",
            [
                {"ip": "10.0.0.1", "hostname": "a.b"},
                {"ip": "10.0.0.2", "hostname": "a_b"},
            ],
        ),
        ("web", [{"ip": "192.168.7.10", "open_ports": [80]}] * 2),
        ("ssh", [{"ip": "192.168.7.10", "open_ports": [22]}] * 2),
        ("database", [{"ip": "192.168.7.10", "open_ports": [5432]}] * 2),
    ],
    ids=["ping-twice", "ping-names-meet", "web-twice", "ssh-twice", "database-twice"],
)
async def test_one_scan_never_makes_two_checks_of_one_name(
    db: AsyncSession, client: AsyncClient, kind: str, hosts: list[dict]
) -> None:
    editor = await _as(db, "editor")
    result = {**SCAN_RESULT, "discovered_hosts": hosts}
    job_id = await _job(db, status=JobStatus.COMPLETED, result=result)
    target = await _agent(db)

    resp = await client.post(
        f"{JOBS}/{job_id}/checks",
        json={"kind": kind, "agent_id": str(target)},
        headers=editor,
    )

    assert resp.status_code == 201, resp.text
    assert resp.json() == {"created": 1, "skipped": 1}
    names = [c.display_name for c in await _checks(db, target)]
    assert len(names) == len(set(names)) == 1


@pytest.mark.parametrize(
    ("kind", "result"),
    [
        ("ping", {"discovered_hosts": None}),
        ("ping", {"discovered_hosts": ["192.168.7.10", {"ip": "192.168.7.11"}]}),
        ("ping", {"discovered_hosts": {"ip": "192.168.7.10"}}),
        (
            "web",
            {
                **SCAN_RESULT,
                "discovered_hosts": [{"ip": "192.168.7.10", "open_ports": "80"}],
            },
        ),
        (
            "web",
            {
                **SCAN_RESULT,
                "discovered_hosts": [{"ip": "192.168.7.10", "open_ports": None}],
            },
        ),
        (
            "web",
            {
                "discovered_hosts": [{"ip": "192.168.7.10", "open_ports": [80]}],
                "scan_params": None,
            },
        ),
    ],
    ids=[
        "hosts-null",
        "host-not-a-mapping",
        "hosts-a-mapping",
        "ports-a-string",
        "ports-null",
        "scan-params-null",
    ],
)
async def test_a_malformed_scan_result_makes_only_the_checks_it_can(
    db: AsyncSession, client: AsyncClient, kind: str, result: dict
) -> None:
    editor = await _as(db, "editor")
    job_id = await _job(db, status=JobStatus.COMPLETED, result=result)
    target = await _agent(db)

    resp = await client.post(
        f"{JOBS}/{job_id}/checks",
        json={"kind": kind, "agent_id": str(target)},
        headers=editor,
    )
    viewed = await client.get(f"{JOBS}/{job_id}", headers=editor)

    assert resp.status_code == 201, resp.text
    assert viewed.status_code == 200, viewed.text
    made = {c.target for c in await _checks(db, target)}
    assert made <= {"192.168.7.11"}
    assert resp.json()["created"] == len(made)
