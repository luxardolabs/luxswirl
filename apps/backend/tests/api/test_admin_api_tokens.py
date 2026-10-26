"""A user's API token opens the admin API as that user, and nothing else does.

The token authenticates only while it is unrevoked and unexpired and its user
is active and unlocked; it carries the user's role; only its SHA-256 is kept;
and the admin API's OpenAPI document is served to any token holder.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import UUID

import pytest
import pytest_asyncio
from app.api.deps import require_api_admin, require_api_editor
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.exceptions import NotFoundException
from app.core.rate_limit import api_token_rate_limit_key, limiter
from app.crud.api_token_crud import ApiTokenCRUD
from app.main import app
from app.models.enum_model import UserRole
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.api_token_core_service import ApiTokenCoreService
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.user_core_service import UserCoreService
from fastapi import HTTPException
from fixtures.factories import make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

pytestmark = pytest.mark.integration

ME = "/api/v1/admin/me"
OPENAPI = "/api/v1/admin/openapi.json"


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    limiter.reset()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as c:
        yield c
    limiter.reset()


async def _user(db: AsyncSession, **overrides):
    user = make_user(**overrides)
    db.add(user)
    await db.flush()
    return user


async def _token(db: AsyncSession, user, **create) -> tuple[str, str]:
    data = ApiTokenCreate(name=create.pop("name", "script"), **create)
    token, plaintext = await ApiTokenCoreService.create_token(db, user.id, data)
    return str(token.id), plaintext


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_a_token_acts_as_its_user_with_their_role(
    db: AsyncSession, client: AsyncClient
) -> None:
    user = await _user(db, role="viewer")
    token_id, plaintext = await _token(db, user, name="ci")

    response = await client.get(ME, headers=_bearer(plaintext))

    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == str(user.id)
    assert body["username"] == user.username
    assert body["role"] == "viewer"
    assert body["token_id"] == token_id
    assert body["token_name"] == "ci"


async def test_only_the_tokens_hash_is_stored_and_its_use_is_stamped(
    db: AsyncSession, client: AsyncClient
) -> None:
    user = await _user(db)
    token_id, plaintext = await _token(db, user)
    row = await ApiTokenCRUD.get_by_hash(
        db, hashlib.sha256(plaintext.encode()).hexdigest()
    )
    assert row is not None and str(row.id) == token_id
    assert row.token_hash != plaintext and plaintext.startswith("lsw_")
    assert row.token_prefix == plaintext[:12]
    assert row.last_used_at is None

    assert (await client.get(ME, headers=_bearer(plaintext))).status_code == 200

    await db.refresh(row)
    assert row.last_used_at is not None


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "lsw_nobearer"},
        {"Authorization": "Basic dXNlcjpwdw=="},
        {"Authorization": "Bearer lsw_" + "x" * 43},
        {"Authorization": "Bearer not-a-token"},
        {"Authorization": "Bearer "},
    ],
)
async def test_no_token_or_an_unknown_one_is_refused(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    response = await client.get(ME, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_a_revoked_token_is_refused(
    db: AsyncSession, client: AsyncClient
) -> None:
    user = await _user(db, role="viewer")
    token_id, plaintext = await _token(db, user)
    await ApiTokenCoreService.revoke_token(db, UUID(token_id), user.id, user.role)

    assert (await client.get(ME, headers=_bearer(plaintext))).status_code == 401


async def test_an_expired_token_is_refused(
    db: AsyncSession, client: AsyncClient
) -> None:
    user = await _user(db)
    _, plaintext = await _token(db, user, expires_in_days=1)
    row = await ApiTokenCRUD.get_by_hash(db, AuthCoreService.hash_token(plaintext))
    assert row is not None
    row.expires_at = utc_now() - timedelta(seconds=1)
    await db.flush()

    assert (await client.get(ME, headers=_bearer(plaintext))).status_code == 401


async def test_a_token_of_a_deactivated_user_is_refused(
    db: AsyncSession, client: AsyncClient
) -> None:
    user = await _user(db)
    _, plaintext = await _token(db, user)
    assert (await client.get(ME, headers=_bearer(plaintext))).status_code == 200

    user.is_active = False
    await db.flush()

    assert (await client.get(ME, headers=_bearer(plaintext))).status_code == 401


async def test_a_session_cookie_does_not_open_the_admin_api(
    db: AsyncSession, client: AsyncClient
) -> None:
    user = await _user(db)
    _, session_token = await AuthCoreService().create_session(db, user.id)
    client.cookies.set(settings.security.session_cookie_name, session_token)

    assert (await client.get(ME)).status_code == 401
    assert (await client.get(ME, headers=_bearer(session_token))).status_code == 401


async def test_a_token_carries_no_more_than_its_users_role(db: AsyncSession) -> None:
    viewer = await _user(db, role="viewer")
    editor = await _user(db, role="editor")
    admin = await _user(db, role="admin")
    principals = {}
    for user in (viewer, editor, admin):
        _, plaintext = await _token(db, user)
        principals[user.role] = await ApiTokenCoreService.authenticate(db, plaintext)

    with pytest.raises(HTTPException) as refused:
        await require_api_editor(principals["viewer"])
    assert refused.value.status_code == 403
    for role in ("viewer", "editor"):
        with pytest.raises(HTTPException) as refused:
            await require_api_admin(principals[role])
        assert refused.value.status_code == 403
    assert await require_api_editor(principals["editor"]) is principals["editor"]
    assert await require_api_admin(principals["admin"]) is principals["admin"]


async def test_a_user_cannot_revoke_another_users_token_but_an_admin_can(
    db: AsyncSession, client: AsyncClient
) -> None:
    owner = await _user(db, role="editor")
    other = await _user(db, role="editor")
    admin = await _user(db, role="admin")
    token_id, plaintext = await _token(db, owner)

    with pytest.raises(NotFoundException):
        await ApiTokenCoreService.revoke_token(
            db, UUID(token_id), other.id, UserRole.EDITOR
        )
    assert (await client.get(ME, headers=_bearer(plaintext))).status_code == 200

    await ApiTokenCoreService.revoke_token(db, UUID(token_id), admin.id, UserRole.ADMIN)
    assert (await client.get(ME, headers=_bearer(plaintext))).status_code == 401


async def test_the_admin_openapi_document_is_for_token_holders_and_the_admin_api_only(
    db: AsyncSession, client: AsyncClient
) -> None:
    assert (await client.get(OPENAPI)).status_code == 401

    user = await _user(db, role="viewer")
    _, plaintext = await _token(db, user)
    response = await client.get(OPENAPI, headers=_bearer(plaintext))

    assert response.status_code == 200
    doc = response.json()
    assert ME in doc["paths"]
    assert all(path.startswith("/api/v1/admin/") for path in doc["paths"])
    assert doc["components"]["securitySchemes"]["apiToken"]["scheme"] == "bearer"


async def test_deleting_a_user_deletes_their_tokens(
    db: AsyncSession, client: AsyncClient
) -> None:
    owner = await _user(db, role="editor")
    await _user(db, role="admin")  # the last active admin is never the one deleted
    _, plaintext = await _token(db, owner)

    assert await UserCoreService().delete_user(db, owner.id)
    await db.flush()

    assert (
        await ApiTokenCRUD.get_by_hash(db, AuthCoreService.hash_token(plaintext))
        is None
    )
    assert (await client.get(ME, headers=_bearer(plaintext))).status_code == 401


async def test_changing_a_password_revokes_the_users_tokens(
    db: AsyncSession, client: AsyncClient
) -> None:
    # A token minted from a hijacked session must not outlive the password
    # change that ends the session.
    user = await _user(db)
    _, plaintext = await _token(db, user)

    await AuthCoreService().change_password(
        db, user.id, "TestPass123!", "N3w-Strong-Passw0rd!x"
    )
    await db.flush()

    assert (await client.get(ME, headers=_bearer(plaintext))).status_code == 401


async def test_an_admin_password_reset_revokes_the_users_tokens(
    db: AsyncSession, client: AsyncClient
) -> None:
    user = await _user(db, role="editor")
    _, plaintext = await _token(db, user)

    await AuthCoreService().reset_password(
        db, user.id, "R3set-Strong-Passw0rd!", force_change=False
    )
    await db.flush()

    assert (await client.get(ME, headers=_bearer(plaintext))).status_code == 401


async def test_a_user_who_must_change_their_password_gets_no_api_access(
    db: AsyncSession, client: AsyncClient
) -> None:
    user = await _user(db)
    _, plaintext = await _token(db, user)
    user.must_change_password = True
    await db.flush()

    assert (await client.get(ME, headers=_bearer(plaintext))).status_code == 401


async def test_failed_logins_by_a_stranger_do_not_switch_off_a_users_tokens(
    db: AsyncSession, client: AsyncClient
) -> None:
    # The login lockout guards the password; a token is not the password.
    user = await _user(db, role="editor")
    _, plaintext = await _token(db, user)
    auth = AuthCoreService()
    for _ in range(AuthCoreService.MAX_FAILED_ATTEMPTS):
        assert await auth.authenticate_user(db, user.username, "wrong") is None
    await db.flush()
    await db.refresh(user)
    assert user.is_locked

    assert (await client.get(ME, headers=_bearer(plaintext))).status_code == 200


async def test_the_admin_openapi_document_holds_only_the_admin_apis_schemas(
    db: AsyncSession, client: AsyncClient
) -> None:
    user = await _user(db, role="viewer")
    _, plaintext = await _token(db, user)

    doc = (await client.get(OPENAPI, headers=_bearer(plaintext))).json()

    schemas = set(doc.get("components", {}).get("schemas", {}))
    assert "ApiWhoAmIResponse" in schemas
    # The agent protocol's and the app's own schemas belong to other documents.
    assert not schemas & {
        "AgentHeartbeat",
        "AgentRegisterRequest",
        "AgentReportSubmission",
        "CheckArtifactCreate",
        "JobResultSubmit",
        "AgentKeyRecoveryResponse",
        "HealthResponse",
        "ApiInfoResponse",
    }


@pytest.mark.parametrize("name", ["a\x00b", "tab\there", "line\nbreak", "del\x7f"])
def test_a_token_name_with_a_control_character_is_refused(name: str) -> None:
    with pytest.raises(ValueError):
        ApiTokenCreate(name=name)


def _request(authorization: str) -> Request:
    return Request(
        {"type": "http", "headers": [(b"authorization", authorization.encode())]}
    )


def test_the_rate_limit_key_is_not_the_stored_token_hash() -> None:
    # The limiter's storage and its log line must not hold the database value.
    key = api_token_rate_limit_key(_request("Bearer lsw_abc"))
    assert AuthCoreService.hash_token("lsw_abc") not in key
    assert key == api_token_rate_limit_key(_request("Bearer  lsw_abc "))
