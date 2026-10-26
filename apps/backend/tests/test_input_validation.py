"""What an input may carry: one-line text, a real IP address, a bounded tag list.

An agent reports its own hostname, version and tags, and operators type names
and tags into forms; every page that names an agent or a check shows them. So
each input schema refuses a control character in that text, an IP address that
does not parse, more than 100 tags and a tag longer than 100 characters. The
response schemas read stored rows and accept what is already there.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.exceptions import validation_message
from app.core.rate_limit import limiter
from app.main import app
from app.models.agent_model import Agent
from app.schemas.agent_schema import (
    AgentCreate,
    AgentHeartbeat,
    AgentInDB,
    AgentRegisterRequest,
    AgentUpdate,
)
from app.schemas.check_schema import BulkCheckCreateRequest, CheckCreate, CheckUpdate
from app.schemas.import_export_schema import CheckExport
from app.schemas.job_schema import JobCreate
from app.schemas.registration_key_schema import RegistrationKeyCreate
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.registration_key_core_service import RegistrationKeyCoreService
from app.web.template_filters import format_datetime
from fixtures.factories import make_agent, make_user
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

_AGENT_TEXT: list[tuple[type, dict[str, Any], str]] = [
    (AgentRegisterRequest, {}, "hostname"),
    (AgentRegisterRequest, {}, "version"),
    (
        AgentHeartbeat,
        {"agent_id": uuid4(), "timestamp": datetime.now(UTC)},
        "hostname",
    ),
    (
        AgentHeartbeat,
        {"agent_id": uuid4(), "timestamp": datetime.now(UTC)},
        "version",
    ),
    (AgentCreate, {}, "agent_name"),
    (AgentCreate, {}, "hostname"),
    (AgentUpdate, {}, "agent_name"),
    (AgentUpdate, {}, "hostname"),
]

_IP_INPUTS: list[tuple[type, dict[str, Any]]] = [
    (AgentRegisterRequest, {}),
    (AgentHeartbeat, {"agent_id": uuid4(), "timestamp": datetime.now(UTC)}),
    (AgentCreate, {}),
    (AgentUpdate, {}),
]

_TAG_INPUTS: list[tuple[type, dict[str, Any]]] = [
    (AgentRegisterRequest, {}),
    (AgentCreate, {}),
    (AgentUpdate, {}),
    (
        CheckCreate,
        {"display_name": "c", "check_type": "http", "target": "https://example.com"},
    ),
    (CheckUpdate, {}),
    (BulkCheckCreateRequest, {"url": "https://example.com"}),
    (JobCreate, {"job_type": "network_scan"}),
    (CheckExport, {"name": "c", "check_type": "http", "target": "https://e.com"}),
]


def _ids(cases: list[tuple[Any, ...]]) -> list[str]:
    return [f"{c[0].__name__}.{c[2]}" if len(c) > 2 else c[0].__name__ for c in cases]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("schema", "base", "field"), _AGENT_TEXT, ids=_ids(_AGENT_TEXT)
)
@pytest.mark.parametrize(
    "bad",
    [
        "web01\nedge",
        "a\x00b",
        "a\x1b[31mb",
        "a\x7fb",
        "web01\u0085edge",  # NEL: str.splitlines() breaks on it
        "a\x9b31mb",  # 8-bit CSI
        "web01\u2028edge",  # line separator
        "web01\u2029edge",  # paragraph separator
        "admin\u202egpj.exe",  # right-to-left override
        "web\u200b01",  # zero-width space
    ],
)
def test_text_with_a_control_character_is_refused(
    schema: type, base: dict[str, Any], field: str, bad: str
) -> None:
    with pytest.raises(ValidationError, match="control characters"):
        schema(**base, **{field: bad})


@pytest.mark.unit
@pytest.mark.parametrize(
    ("schema", "base", "field"), _AGENT_TEXT, ids=_ids(_AGENT_TEXT)
)
def test_one_line_text_is_kept(schema: type, base: dict[str, Any], field: str) -> None:
    assert getattr(schema(**base, **{field: "  web01 edge.example  "}), field) == (
        "web01 edge.example"
    )


@pytest.mark.unit
@pytest.mark.parametrize(("schema", "base"), _IP_INPUTS, ids=_ids(_IP_INPUTS))
@pytest.mark.parametrize(
    "bad", ["not-an-ip", "10.0.0.256", "10.0.0", "::g", "1.2.3.4/24"]
)
def test_an_ip_address_that_does_not_parse_is_refused(
    schema: type, base: dict[str, Any], bad: str
) -> None:
    with pytest.raises(ValidationError, match="not a valid IP address"):
        schema(**base, ip_address=bad)


@pytest.mark.unit
@pytest.mark.parametrize(("schema", "base"), _IP_INPUTS, ids=_ids(_IP_INPUTS))
@pytest.mark.parametrize("blank", ["", "   ", None])
def test_a_blank_ip_address_is_unset(
    schema: type, base: dict[str, Any], blank: str | None
) -> None:
    """An agent config's `ip_address: ${UNSET}` expands to an empty string."""
    assert schema(**base, ip_address=blank).ip_address is None


@pytest.mark.unit
@pytest.mark.parametrize(("schema", "base"), _IP_INPUTS, ids=_ids(_IP_INPUTS))
@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("10.0.1.100", "10.0.1.100"),
        ("2001:0db8:0000:0000:0000:0000:0000:0001", "2001:db8::1"),
        ("::ffff:10.0.0.1", "::ffff:10.0.0.1"),
        ("fe80::1%eth0", "fe80::1%eth0"),
    ],
)
def test_an_ip_address_is_stored_in_canonical_form(
    schema: type, base: dict[str, Any], given: str, stored: str
) -> None:
    assert schema(**base, ip_address=given).ip_address == stored


@pytest.mark.unit
@pytest.mark.parametrize(("schema", "base"), _TAG_INPUTS, ids=_ids(_TAG_INPUTS))
def test_more_than_100_tags_are_refused(schema: type, base: dict[str, Any]) -> None:
    assert len(schema(**base, tags=[f"t{i}" for i in range(100)]).tags) == 100
    with pytest.raises(ValidationError, match="at most 100 items"):
        schema(**base, tags=[f"t{i}" for i in range(101)])


@pytest.mark.unit
@pytest.mark.parametrize(("schema", "base"), _TAG_INPUTS, ids=_ids(_TAG_INPUTS))
def test_a_long_or_multiline_tag_is_refused(schema: type, base: dict[str, Any]) -> None:
    assert schema(**base, tags=["x" * 100]).tags == ["x" * 100]
    with pytest.raises(ValidationError, match="at most 100 characters"):
        schema(**base, tags=["x" * 101])
    with pytest.raises(ValidationError, match="control characters"):
        schema(**base, tags=["prod\nweb"])


@pytest.mark.unit
def test_a_stored_row_with_old_values_still_reads() -> None:
    """The response schemas are not input: a stored row reads back as it is."""
    agent = make_agent(
        hostname="web01\nedge",
        ip_address="unknown",
        tags=[f"t{i}" for i in range(150)],
        created_at=utc_now(),
        updated_at=utc_now(),
    )

    row = AgentInDB.model_validate(agent)

    assert (row.hostname, row.ip_address, len(row.tags or [])) == (
        "web01\nedge",
        "unknown",
        150,
    )


@pytest.mark.unit
@pytest.mark.parametrize("zone", ["Not/AZone", "../etc/passwd", ""])
def test_an_unknown_timezone_shows_utc_and_leaves_a_trace(
    zone: str, caplog: pytest.LogCaptureFixture
) -> None:
    dt = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)

    with caplog.at_level(logging.DEBUG, logger="luxswirl.web.template_filters"):
        shown = format_datetime(dt, "%H:%M %Z", timezone=zone)

    assert shown == "03:04 UTC"
    assert any("Unknown timezone" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Through the routes the values arrive on.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def agent_client(db: AsyncSession, monkeypatch) -> AsyncIterator[AsyncClient]:
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
async def reg_key(db: AsyncSession) -> str:
    _, token = await RegistrationKeyCoreService.create_key(
        db, RegistrationKeyCreate(name="input-validation"), created_by="tests"
    )
    return token


@pytest.mark.integration
async def test_registration_with_a_newline_in_the_hostname_is_refused(
    agent_client: AsyncClient, reg_key: str, db: AsyncSession
) -> None:
    before = len((await db.execute(select(Agent.id))).all())

    resp = await agent_client.post(
        "/api/v1/agent/register",
        json={"hostname": "web01\nedge"},
        headers={"Authorization": f"Bearer {reg_key}"},
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"] == "VALIDATION_ERROR"
    assert len((await db.execute(select(Agent.id))).all()) == before


@pytest.mark.integration
async def test_a_heartbeat_with_a_newline_in_the_hostname_is_refused(
    agent_client: AsyncClient, reg_key: str, db: AsyncSession
) -> None:
    agent = make_agent(approval_status="pending", hostname="web01")
    db.add(agent)
    await db.flush()

    resp = await agent_client.post(
        "/api/v1/agent/heartbeat",
        json={
            "agent_id": str(agent.id),
            "timestamp": utc_now().isoformat(),
            "status": "online",
            "hostname": "web01\nedge",
        },
        headers={"Authorization": f"Bearer {reg_key}"},
    )

    assert resp.status_code == 422, resp.text
    await db.refresh(agent)
    assert agent.hostname == "web01"


@pytest.mark.integration
async def test_a_refused_heartbeat_names_each_error_once(
    agent_client: AsyncClient, reg_key: str, db: AsyncSession
) -> None:
    agent = make_agent(approval_status="pending", hostname="web01")
    db.add(agent)
    await db.flush()

    resp = await agent_client.post(
        "/api/v1/agent/heartbeat",
        json={
            "agent_id": str(agent.id),
            "timestamp": utc_now().isoformat(),
            "status": "online",
            "hostname": "web01\nedge",
        },
        headers={"Authorization": f"Bearer {reg_key}"},
    )

    assert resp.status_code == 422, resp.text
    locs = [tuple(e["loc"]) for e in resp.json()["detail"]]
    assert len(locs) == len(set(locs)) == 1, locs


@pytest_asyncio.fixture
async def web_client(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    admin = make_user(role="admin")
    db.add(admin)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, admin.id)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as c:
        c.cookies.set(settings.security.session_cookie_name, token)
        yield c


@pytest.mark.integration
@pytest.mark.parametrize(
    ("form", "says"),
    [
        ({"heartbeat_interval": "abc"}, "heartbeat_interval must be a whole number"),
        ({"heartbeat_interval": "-5"}, "heartbeat_interval"),
        ({"report_backpressure_threshold": "lots"}, "must be a number"),
        ({"agent_name": "web01\nedge"}, "control characters"),
        ({"tags": ",".join(f"t{i}" for i in range(101))}, "at most 100 items"),
    ],
)
async def test_the_agent_form_refuses_a_value_it_cannot_store(
    web_client: AsyncClient, db: AsyncSession, form: dict[str, str], says: str
) -> None:
    """A value the form cannot store is refused with the reason, not dropped while
    the page reports success."""
    agent = make_agent(heartbeat_interval=30, agent_name="keep-me")
    db.add(agent)
    await db.flush()

    resp = await web_client.patch(f"/agents/{agent.id}", data=form)

    assert resp.status_code == 400, resp.text
    assert says in resp.text
    assert "updated successfully" not in resp.text
    await db.refresh(agent)
    assert (agent.heartbeat_interval, agent.agent_name) == (30, "keep-me")


@pytest.mark.integration
@pytest.mark.parametrize(
    ("form", "says"),
    [
        (
            {"agent_name": "web01\nedge"},
            "agent_name: Value error, must not contain control characters",
        ),
        (
            {"tags": ",".join(f"t{i}" for i in range(101))},
            "tags: List should have at most 100 items after validation, not 101",
        ),
    ],
)
async def test_a_form_refused_by_its_schema_reads_as_field_and_reason(
    web_client: AsyncClient, db: AsyncSession, form: dict[str, str], says: str
) -> None:
    """The operator sees which field and why, not pydantic's own report."""
    agent = make_agent(agent_name="keep-me")
    db.add(agent)
    await db.flush()

    resp = await web_client.patch(f"/agents/{agent.id}", data=form)

    assert resp.status_code == 400, resp.text
    assert says in resp.text
    for noise in ("validation error for", "input_value", "errors.pydantic.dev"):
        assert noise not in resp.text


@pytest.mark.unit
def test_a_refused_ip_address_is_not_echoed_in_the_message() -> None:
    """The message reaches a toast, so the refused text is not repeated in it."""
    payload = "<img src=x onerror=alert(1)>"
    with pytest.raises(ValidationError) as refused:
        AgentUpdate(ip_address=payload)

    message = validation_message(refused.value)

    assert message == "ip_address: Value error, not a valid IP address"


@pytest.mark.unit
def test_a_whole_model_refusal_is_its_reason_alone() -> None:
    class Pair(BaseModel):
        a: int = 0

        @model_validator(mode="after")
        def check(self) -> Pair:
            raise ValueError("a and b disagree")

    with pytest.raises(ValidationError) as refused:
        Pair()

    assert validation_message(refused.value) == "Value error, a and b disagree"


@pytest.mark.integration
async def test_the_agent_form_still_clears_a_setting_with_zero(
    web_client: AsyncClient, db: AsyncSession
) -> None:
    agent = make_agent(heartbeat_interval=30)
    db.add(agent)
    await db.flush()

    resp = await web_client.patch(
        f"/agents/{agent.id}", data={"heartbeat_interval": "0"}
    )

    assert resp.status_code == 200, resp.text
    await db.refresh(agent)
    assert agent.heartbeat_interval is None


@pytest_asyncio.fixture
async def approved(db: AsyncSession) -> tuple[Agent, str]:
    agent = make_agent(approval_status="pending", tags=["kept"])
    db.add(agent)
    await db.flush()
    _, key = await AgentCoreService.approve_agent(db, agent.id)
    return agent, key


def _beat(agent_id: object, **fields: object) -> dict[str, object]:
    return {
        "agent_id": str(agent_id),
        "timestamp": utc_now().isoformat(),
        "status": "online",
        **fields,
    }


_OVER_LIMIT_TAGS = pytest.mark.parametrize(
    "tags",
    [[f"t{i}" for i in range(101)], ["x" * 101], ["prod\nweb"]],
    ids=["101-tags", "101-char-tag", "multiline-tag"],
)


@pytest.mark.integration
@_OVER_LIMIT_TAGS
async def test_an_approved_agents_heartbeat_is_never_refused_over_its_tags(
    agent_client: AsyncClient, db: AsyncSession, approved: tuple[Agent, str], tags
) -> None:
    """After approval the server ignores declared tags; refusing the heartbeat
    over them would show the agent offline and stop its config updates."""
    agent, key = approved

    resp = await agent_client.post(
        "/api/v1/agent/heartbeat",
        json=_beat(agent.id, tags=tags),
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 200, resp.text
    await db.refresh(agent)
    assert agent.tags == ["kept"]


@pytest.mark.integration
@_OVER_LIMIT_TAGS
async def test_a_pending_agents_over_limit_tags_are_not_stored(
    agent_client: AsyncClient,
    reg_key: str,
    db: AsyncSession,
    caplog: pytest.LogCaptureFixture,
    tags,
) -> None:
    agent = make_agent(approval_status="pending", tags=["suggested"])
    db.add(agent)
    await db.flush()

    with caplog.at_level(logging.WARNING, logger="luxswirl.services.agent"):
        resp = await agent_client.post(
            "/api/v1/agent/heartbeat",
            json=_beat(agent.id, tags=tags),
            headers={"Authorization": f"Bearer {reg_key}"},
        )

    assert resp.status_code == 200, resp.text
    await db.refresh(agent)
    assert agent.tags == ["suggested"]
    assert any("over the tag limits" in r.getMessage() for r in caplog.records)


@pytest.mark.integration
async def test_a_pending_agents_tags_within_the_limits_are_stored(
    agent_client: AsyncClient, reg_key: str, db: AsyncSession
) -> None:
    agent = make_agent(approval_status="pending", tags=["suggested"])
    db.add(agent)
    await db.flush()

    resp = await agent_client.post(
        "/api/v1/agent/heartbeat",
        json=_beat(agent.id, tags=["prod", "web"]),
        headers={"Authorization": f"Bearer {reg_key}"},
    )

    assert resp.status_code == 200, resp.text
    await db.refresh(agent)
    assert agent.tags == ["prod", "web"]


@pytest.mark.integration
async def test_registration_with_an_empty_ip_address_stores_it_unset(
    agent_client: AsyncClient, reg_key: str, db: AsyncSession
) -> None:
    resp = await agent_client.post(
        "/api/v1/agent/register",
        json={"hostname": "blank-ip", "ip_address": ""},
        headers={"Authorization": f"Bearer {reg_key}"},
    )

    assert resp.status_code == 201, resp.text
    stored = await db.get(Agent, UUID(resp.json()["agent_id"]))
    assert stored is not None and stored.ip_address is None


@pytest.mark.integration
async def test_registration_with_a_nel_in_the_hostname_is_refused(
    agent_client: AsyncClient, reg_key: str
) -> None:
    resp = await agent_client.post(
        "/api/v1/agent/register",
        json={"hostname": "web01\u0085edge"},
        headers={"Authorization": f"Bearer {reg_key}"},
    )

    assert resp.status_code == 422, resp.text
