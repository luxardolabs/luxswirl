"""Users on the admin API: an admin's alone.

No response carries a password or its hash. The rules hold by either path:
the password policy, the last active admin, no deleting your own account, and
a password reset ending the user's sessions and API tokens.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.exceptions import AuthorizationException, ValidationException
from app.core.rate_limit import limiter
from app.main import app
from app.models.user_model import User
from app.schemas.admin_user_schema import AdminUserCreate
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.admin_bootstrap_core_service import AdminBootstrapCoreService
from app.services.core.admin_user_core_service import AdminUserCoreService
from app.services.core.api_token_core_service import ApiTokenCoreService
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.settings_core_service import SettingsCoreService
from app.services.core.user_core_service import UserCoreService
from fixtures.factories import make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

USERS = "/api/v1/admin/users"
GOOD_PASSWORD = "Correct-Horse-9"


@pytest_asyncio.fixture
async def client(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    await SettingsCoreService.ensure_default_settings(db)
    limiter.reset()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as c:
        yield c
    limiter.reset()


async def _user(
    db: AsyncSession, role: str, **overrides: object
) -> tuple[User, dict[str, str]]:
    user = make_user(role=role, **overrides)
    db.add(user)
    await db.flush()
    _, plaintext = await ApiTokenCoreService.create_token(
        db, user.id, ApiTokenCreate(name=role, scopes=["*"])
    )
    return user, {"Authorization": f"Bearer {plaintext}"}


async def _as(db: AsyncSession, role: str) -> dict[str, str]:
    return (await _user(db, role))[1]


async def _target(db: AsyncSession, **overrides: object) -> UUID:
    user = make_user(**overrides)
    db.add(user)
    await db.flush()
    return user.id


# ── Who may ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["viewer", "editor"])
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "", None),
        ("GET", "/{id}", None),
        ("POST", "", {"username": "made_by_x", "password": GOOD_PASSWORD}),
        ("PATCH", "/{id}", {"role": "admin"}),
        ("DELETE", "/{id}", None),
        ("POST", "/{id}/reset-password", {"new_password": GOOD_PASSWORD}),
        ("POST", "/{id}/unlock", None),
    ],
)
async def test_only_an_admin_reaches_users(
    db: AsyncSession,
    client: AsyncClient,
    role: str,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    target = await _target(db, role="viewer")
    other = await _as(db, role)

    resp = await client.request(
        method, USERS + path.format(id=target), json=body, headers=other
    )

    assert resp.status_code == 403, resp.text
    db.expire_all()
    row = await db.get(User, target)
    assert row is not None and row.role == "viewer"
    made = await db.scalar(
        select(func.count()).select_from(User).where(User.username == "made_by_x")
    )
    assert made == 0


@pytest.mark.parametrize("role", ["viewer", "editor"])
async def test_the_core_refuses_a_change_by_a_non_admin_whatever_the_path(
    db: AsyncSession, role: str
) -> None:
    actor = await _target(db, role=role)
    target = await _target(db)

    with pytest.raises(AuthorizationException):
        await AdminUserCoreService.create_user(
            db,
            actor,
            "someone",
            AdminUserCreate(username="by_core", password=GOOD_PASSWORD),
        )
    with pytest.raises(AuthorizationException):
        await AdminUserCoreService.delete_user(db, actor, target)

    assert await db.get(User, target) is not None


async def test_no_token_is_refused(client: AsyncClient) -> None:
    assert (await client.get(USERS)).status_code == 401


# ── Reads ───────────────────────────────────────────────────────────────────


async def test_users_are_listed_and_read_without_their_password(
    db: AsyncSession, client: AsyncClient
) -> None:
    admin = await _as(db, "admin")
    target = await _target(db, role="editor", full_name="Ed Itor")

    listed = await client.get(USERS, params={"role": "editor"}, headers=admin)
    unfiltered = await client.get(
        USERS, params={"role": "", "is_active": ""}, headers=admin
    )
    one = await client.get(f"{USERS}/{target}", headers=admin)

    assert listed.status_code == 200, listed.text
    assert {u["role"] for u in listed.json()["items"]} == {"editor"}
    assert unfiltered.json()["total"] >= listed.json()["total"]
    assert one.json()["full_name"] == "Ed Itor"
    assert "password_hash" not in one.json()
    assert all("password_hash" not in u for u in listed.json()["items"])


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "", None),
        ("PATCH", "", {"full_name": "x"}),
        ("DELETE", "", None),
        ("POST", "/reset-password", {"new_password": GOOD_PASSWORD}),
        ("POST", "/unlock", None),
    ],
)
async def test_an_unknown_user_is_404(
    db: AsyncSession,
    client: AsyncClient,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    admin = await _as(db, "admin")

    resp = await client.request(
        method, f"{USERS}/{uuid4()}{path}", json=body, headers=admin
    )

    assert resp.status_code == 404, resp.text


# ── Create ──────────────────────────────────────────────────────────────────


async def test_a_new_user_is_made_by_the_token_s_admin(
    db: AsyncSession, client: AsyncClient
) -> None:
    admin_user, admin = await _user(db, "admin")
    admin_name = admin_user.username

    resp = await client.post(
        USERS,
        json={"username": "new.ops", "password": GOOD_PASSWORD, "role": "editor"},
        headers=admin,
    )

    assert resp.status_code == 201, resp.text
    made = resp.json()
    assert made["role"] == "editor" and made["must_change_password"] is True
    assert made["created_by"] == admin_name
    assert GOOD_PASSWORD not in resp.text and "password_hash" not in made


@pytest.mark.parametrize(
    "body",
    [
        {"username": "weak", "password": "alllowercase"},
        {"username": "bad name", "password": GOOD_PASSWORD},
        {"username": "xyz", "password": GOOD_PASSWORD, "role": "root"},
        {"username": "extra", "password": GOOD_PASSWORD, "is_superuser": True},
    ],
)
async def test_a_user_the_api_cannot_make_is_refused(
    db: AsyncSession, client: AsyncClient, body: dict[str, object]
) -> None:
    admin = await _as(db, "admin")

    resp = await client.post(USERS, json=body, headers=admin)

    assert resp.status_code == 422, resp.text


async def test_a_taken_username_is_a_409(db: AsyncSession, client: AsyncClient) -> None:
    admin = await _as(db, "admin")
    await _target(db, username="taken")

    resp = await client.post(
        USERS, json={"username": "taken", "password": GOOD_PASSWORD}, headers=admin
    )

    assert resp.status_code == 409, resp.text
    assert "$2" not in resp.text  # no bcrypt hash from the database's own error


# ── Change ──────────────────────────────────────────────────────────────────


async def test_a_change_sets_what_it_sends(
    db: AsyncSession, client: AsyncClient
) -> None:
    admin = await _as(db, "admin")
    target = await _target(db, role="viewer", full_name="Old Name")

    resp = await client.patch(
        f"{USERS}/{target}", json={"role": "editor", "full_name": None}, headers=admin
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "editor" and resp.json()["full_name"] is None
    assert resp.json()["is_active"] is True


@pytest.mark.parametrize("field", ["role", "is_active", "must_change_password"])
async def test_a_required_field_is_not_cleared(
    db: AsyncSession, client: AsyncClient, field: str
) -> None:
    admin = await _as(db, "admin")
    target = await _target(db)

    resp = await client.patch(f"{USERS}/{target}", json={field: None}, headers=admin)

    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize("change", [{"role": "editor"}, {"is_active": False}])
async def test_the_last_active_admin_stays_one(
    db: AsyncSession, client: AsyncClient, change: dict[str, object]
) -> None:
    await db.execute(update(User).values(role="viewer"))
    admin_user, admin = await _user(db, "admin")
    admin_id = admin_user.id

    resp = await client.patch(f"{USERS}/{admin_id}", json=change, headers=admin)

    assert resp.status_code == 409, resp.text
    db.expire_all()
    row = await db.get(User, admin_id)
    assert row is not None and row.role == "admin" and row.is_active is True


# ── Delete ──────────────────────────────────────────────────────────────────


async def test_an_admin_deletes_a_user(db: AsyncSession, client: AsyncClient) -> None:
    admin = await _as(db, "admin")
    target = await _target(db)

    resp = await client.delete(f"{USERS}/{target}", headers=admin)

    assert resp.status_code == 204, resp.text
    db.expire_all()
    assert await db.get(User, target) is None


async def test_no_one_deletes_their_own_account(
    db: AsyncSession, client: AsyncClient
) -> None:
    await _target(db, role="admin")  # another admin: the last-admin rule is not it
    admin_user, admin = await _user(db, "admin")
    admin_id = admin_user.id

    resp = await client.delete(f"{USERS}/{admin_id}", headers=admin)

    assert resp.status_code == 422, resp.text
    db.expire_all()
    assert await db.get(User, admin_id) is not None


async def test_the_core_refuses_a_self_delete_whatever_the_path(
    db: AsyncSession,
) -> None:
    await _target(db, role="admin")
    me = await _target(db, role="admin")

    with pytest.raises(ValidationException, match="your own account"):
        await UserCoreService().delete_user(db, me, actor_id=me)


# ── Reset and unlock ────────────────────────────────────────────────────────


@pytest.mark.parametrize("must_change", [True, False])
async def test_a_reset_ends_the_user_s_sessions_and_tokens(
    db: AsyncSession, client: AsyncClient, must_change: bool
) -> None:
    admin = await _as(db, "admin")
    target, target_token = await _user(db, "viewer", must_change_password=False)
    target_id = target.id
    auth = AuthCoreService()
    _, session_token = await auth.create_session(db, target_id)
    assert await auth.verify_session(db, session_token) is not None

    resp = await client.post(
        f"{USERS}/{target_id}/reset-password",
        json={"new_password": GOOD_PASSWORD, "must_change": must_change},
        headers=admin,
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["must_change_password"] is must_change
    assert await auth.verify_session(db, session_token) is None
    me = await client.get("/api/v1/admin/me", headers=target_token)
    assert me.status_code == 401
    assert GOOD_PASSWORD not in resp.text


async def test_a_reset_to_a_weak_password_is_refused(
    db: AsyncSession, client: AsyncClient
) -> None:
    admin = await _as(db, "admin")
    target = await _target(db)

    resp = await client.post(
        f"{USERS}/{target}/reset-password",
        json={"new_password": "alllowercase"},
        headers=admin,
    )

    assert resp.status_code == 422, resp.text


async def test_unlock_clears_the_lock(db: AsyncSession, client: AsyncClient) -> None:
    admin = await _as(db, "admin")
    target = await _target(
        db,
        failed_login_attempts=5,
        locked_until=datetime.now(UTC) + timedelta(minutes=30),
    )

    resp = await client.post(f"{USERS}/{target}/unlock", headers=admin)

    assert resp.status_code == 200, resp.text
    assert resp.json()["locked_until"] is None
    assert resp.json()["failed_login_attempts"] == 0


# ── Refused passwords stay secret ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "body", "secret"),
    [
        (
            "",
            {"username": "leaky", "password": "CorrectHorseBatteryStaple"},
            "CorrectHorseBatteryStaple",
        ),
        ("/{id}/reset-password", {"new_password": "Tr0ub4d"}, "Tr0ub4d"),
    ],
)
async def test_a_refused_password_is_neither_logged_nor_echoed(
    db: AsyncSession,
    client: AsyncClient,
    app_logs: list[logging.LogRecord],
    path: str,
    body: dict[str, object],
    secret: str,
) -> None:
    admin = await _as(db, "admin")
    target = await _target(db)

    resp = await client.post(USERS + path.format(id=target), json=body, headers=admin)

    assert resp.status_code == 422, resp.text
    assert secret not in resp.text
    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in app_logs)
    assert "Validation error" in logged
    assert secret not in logged


async def test_a_password_bcrypt_would_cut_short_is_refused_by_policy(
    db: AsyncSession, client: AsyncClient
) -> None:
    admin = await _as(db, "admin")
    target = await _target(db)

    resp = await client.post(
        f"{USERS}/{target}/reset-password",
        json={"new_password": "Aa1-" + "é" * 40},
        headers=admin,
    )

    assert resp.status_code == 422, resp.text
    assert "72 bytes" in resp.text


async def test_a_recovery_reset_ends_the_user_s_api_tokens(
    db: AsyncSession, client: AsyncClient
) -> None:
    victim, stolen_token = await _user(db, "admin", must_change_password=False)
    assert (
        await client.get("/api/v1/admin/me", headers=stolen_token)
    ).status_code == 200
    bootstrap = AdminBootstrapCoreService()

    temporary = await bootstrap.reset_password(db, victim.username)
    await bootstrap.change_password(db, victim.username, temporary, "Brand-New-Pass-42")

    resp = await client.get("/api/v1/admin/me", headers=stolen_token)
    assert resp.status_code == 401, resp.text
