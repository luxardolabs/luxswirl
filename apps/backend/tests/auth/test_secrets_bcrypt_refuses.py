"""A secret bcrypt cannot check is a wrong secret, never an error.

bcrypt refuses to hash or check more than 72 bytes, and a secret holds only text
UTF-8 can encode, so no stored hash is of a longer secret or of one with a lone
surrogate (a multipart form decoded in a charset its client names can carry
one). Any caller can send either: a bearer token to the agent protocol, a
password to the login form. It is refused like any other wrong secret: no ERROR
log line, no 500, and a failed login is counted.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.rate_limit import limiter
from app.main import app
from app.schemas.registration_key_schema import RegistrationKeyCreate
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.registration_key_core_service import RegistrationKeyCoreService
from fixtures.factories import make_agent, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

LONG = "luxswirl_rk_" + "a" * 88  # 100 bytes


def _errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]


def test_an_over_long_registration_key_does_not_match() -> None:
    key_hash = RegistrationKeyCoreService.hash_key(
        RegistrationKeyCoreService.generate_key()
    )

    assert RegistrationKeyCoreService.verify_key(LONG, key_hash) is False


def test_an_over_long_registration_key_logs_no_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    key_hash = RegistrationKeyCoreService.hash_key(
        RegistrationKeyCoreService.generate_key()
    )

    with caplog.at_level(logging.DEBUG):
        RegistrationKeyCoreService.verify_key(LONG, key_hash)

    assert _errors(caplog) == []


def test_an_over_long_password_does_not_match() -> None:
    password_hash = AuthCoreService.hash_password("TestPass123!")

    assert AuthCoreService.verify_password("p" * 100, password_hash) is False


def test_a_72_byte_secret_still_matches() -> None:
    secret = "k" * 72

    assert RegistrationKeyCoreService.verify_key(
        secret, RegistrationKeyCoreService.hash_key(secret)
    )
    assert AuthCoreService.verify_password(
        secret, AuthCoreService.hash_password(secret)
    )


@pytest.mark.integration
async def test_an_over_long_login_password_is_a_failed_login(db: AsyncSession) -> None:
    user = make_user(username="longpass")
    db.add(user)
    await db.flush()

    assert await AuthCoreService().authenticate_user(db, "longpass", "p" * 100) is None
    assert user.failed_login_attempts == 1


@pytest_asyncio.fixture
async def client(db: AsyncSession, monkeypatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    limiter.reset()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest_asyncio.fixture
async def registration_key(db: AsyncSession) -> None:
    await RegistrationKeyCoreService.create_key(
        db, RegistrationKeyCreate(name="over-long-tests"), created_by="tests"
    )


@pytest.mark.integration
@pytest.mark.usefixtures("registration_key")
async def test_registering_with_an_over_long_key_is_refused_without_an_error(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        resp = await client.post(
            "/api/v1/agent/register",
            json={"hostname": "web01"},
            headers={"Authorization": f"Bearer {LONG}"},
        )

    assert resp.status_code == 401, resp.text
    assert _errors(caplog) == []


@pytest.mark.integration
@pytest.mark.usefixtures("registration_key")
async def test_a_heartbeat_with_an_over_long_key_is_refused_without_an_error(
    client: AsyncClient, db: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    agent = make_agent(approval_status="pending")
    db.add(agent)
    await db.flush()
    await AgentCoreService.approve_agent(db, agent.id, actor_is_admin=True)

    with caplog.at_level(logging.DEBUG):
        resp = await client.post(
            "/api/v1/agent/heartbeat",
            json={
                "agent_id": str(agent.id),
                "timestamp": utc_now().isoformat(),
                "status": "online",
            },
            headers={"Authorization": f"Bearer {LONG}"},
        )

    assert resp.status_code == 401, resp.text
    assert _errors(caplog) == []


OWN = settings.allowed_hosts[0]
BOUNDARY = "secretsboundary"


async def _login_multipart(
    client: AsyncClient, username: bytes, password: bytes, charset: str
) -> int:
    body = (
        (
            f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="username"\r\n\r\n'
        ).encode()
        + username
        + (
            f"\r\n--{BOUNDARY}\r\n"
            'Content-Disposition: form-data; name="password"\r\n\r\n'
        ).encode()
        + password
        + f"\r\n--{BOUNDARY}--\r\n".encode()
    )
    resp = await client.post(
        f"https://{OWN}/login",
        content=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={BOUNDARY}; charset={charset}",
            "Origin": f"https://{OWN}",
        },
    )
    return resp.status_code


def test_a_password_with_a_lone_surrogate_does_not_match() -> None:
    password_hash = AuthCoreService.hash_password("TestPass123!")

    assert AuthCoreService.verify_password("\ud800", password_hash) is False


@pytest.mark.integration
async def test_a_password_with_a_lone_surrogate_is_refused_by_policy(
    db: AsyncSession,
) -> None:
    valid, error = await AuthCoreService.validate_password_complexity(
        db, "TestPass123!\ud800"
    )

    assert valid is False
    assert error


@pytest.mark.integration
async def test_a_wrong_multipart_login_is_a_failed_login(
    client: AsyncClient, db: AsyncSession
) -> None:
    db.add(make_user(username="multipartctl"))
    await db.flush()

    status = await _login_multipart(client, b"multipartctl", b"wrong", "utf-8")

    assert status == 401


@pytest.mark.integration
@pytest.mark.parametrize("username", ["surrogate", "nosuchuser"])
async def test_a_login_password_with_a_lone_surrogate_is_a_failed_login(
    client: AsyncClient,
    db: AsyncSession,
    caplog: pytest.LogCaptureFixture,
    username: str,
) -> None:
    db.add(make_user(username="surrogate"))
    await db.flush()

    with caplog.at_level(logging.DEBUG):
        status = await _login_multipart(
            client, username.encode(), b"\\ud800", "unicode_escape"
        )

    assert status == 401
    assert _errors(caplog) == []


@pytest.mark.integration
@pytest.mark.parametrize("username", ["a\x00b", "a\ud800"])
async def test_a_username_no_user_can_have_is_an_unknown_user(
    db: AsyncSession, username: str
) -> None:
    assert await AuthCoreService().authenticate_user(db, username, "nope") is None


@pytest.mark.integration
async def test_a_login_username_with_a_nul_is_a_failed_login(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        resp = await client.post(
            f"https://{OWN}/login",
            data={"username": "a\x00b", "password": "nope"},
            headers={"Origin": f"https://{OWN}"},
        )

    assert resp.status_code == 401
    assert _errors(caplog) == []


@pytest.mark.integration
async def test_a_login_username_with_a_lone_surrogate_is_refused_without_an_error(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        status = await _login_multipart(client, b"a\\ud800", b"nope", "unicode_escape")

    assert 400 <= status < 500
    assert _errors(caplog) == []
