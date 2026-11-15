"""The password policy is the one stored in Settings > Security > Password Complexity.

Each of the six `security.*` complexity settings governs every path that sets a
password (/setup, user create, admin reset, change-password): a rule turned on
is enforced, and a rule turned off is not, with the defaults in
`default_settings.py` when nothing is stored.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.exceptions import ValidationException
from app.core.rate_limit import limiter
from app.main import app
from app.models.user_model import User
from app.schemas.user_schema import UserCreate
from app.services.core import admin_bootstrap_core_service as bootstrap_module
from app.services.core.admin_bootstrap_core_service import AdminBootstrapCoreService
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.settings_core_service import SettingsCoreService
from app.services.core.user_core_service import UserCoreService
from fixtures.factories import make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

# 26 characters, upper, lower, digit; none from the special-character list.
NO_SPECIAL = "A long enough passphrase 7"
# Lowercase and digits only, long enough for /setup's own minimum.
NO_UPPERCASE = "all lowercase pass 1234"


@pytest.fixture(autouse=True)
def token_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "admin-setup-token"
    monkeypatch.setattr(bootstrap_module, "SETUP_TOKEN_FILE", path)
    for name in ("INITIAL_ADMIN_USERNAME", "INITIAL_ADMIN_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"{name}_FILE", raising=False)
    return path


@pytest_asyncio.fixture
async def client(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    limiter.reset()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url=f"https://{settings.allowed_hosts[0]}",
        ) as c:
            yield c
    finally:
        limiter.reset()


async def _store(db: AsyncSession, key: str, value: bool | int) -> None:
    await SettingsCoreService.ensure_default_settings(db)
    await SettingsCoreService.set_setting(db, key, value)
    assert await SettingsCoreService.get_setting(db, key) == value


async def _users(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(User)) or 0)


async def _setup(db: AsyncSession, client: AsyncClient, password: str) -> int:
    token = (await AdminBootstrapCoreService().bootstrap(db)).setup_token
    assert token
    resp = await client.post(
        "/setup",
        data={
            "token": token,
            "login": "owner",
            "password": password,
            "password_confirm": password,
        },
    )
    return resp.status_code


# ── defaults ──────────────────────────────────────────────────────────────────


async def test_the_default_policy_applies_when_nothing_is_stored(
    db: AsyncSession,
) -> None:
    valid, problem = await AuthCoreService.validate_password_complexity(
        db, NO_UPPERCASE
    )
    assert valid is False
    assert "uppercase" in problem
    assert (await AuthCoreService.validate_password_complexity(db, NO_SPECIAL))[0]


async def test_setup_refuses_a_password_the_default_policy_refuses(
    db: AsyncSession, client: AsyncClient
) -> None:
    assert await _setup(db, client, NO_UPPERCASE) == 400
    assert await _users(db) == 0


# ── a stricter stored policy is enforced ──────────────────────────────────────


async def test_a_stored_minimum_length_is_enforced(db: AsyncSession) -> None:
    await _store(db, "security.min_password_length", 32)
    valid, problem = await AuthCoreService.validate_password_complexity(db, NO_SPECIAL)
    assert valid is False, "a 26-char password passed a stored 32-char minimum"
    assert "32" in problem


async def test_setup_enforces_a_stored_require_special_char(
    db: AsyncSession, client: AsyncClient
) -> None:
    await _store(db, "security.require_special_char", True)
    assert await _setup(db, client, NO_SPECIAL) == 400
    assert await _users(db) == 0


async def test_user_create_enforces_a_stored_require_special_char(
    db: AsyncSession,
) -> None:
    await _store(db, "security.require_special_char", True)
    with pytest.raises(ValidationException, match="special character"):
        await UserCoreService().create_user(
            db, UserCreate(username="strict", password="Sup3rSecretpw")
        )


# ── a rule turned off is not enforced ─────────────────────────────────────────


async def test_a_stored_require_uppercase_off_is_honoured(db: AsyncSession) -> None:
    await _store(db, "security.require_uppercase", False)
    valid, problem = await AuthCoreService.validate_password_complexity(
        db, NO_UPPERCASE
    )
    assert valid is True, problem


async def test_setup_honours_a_stored_require_uppercase_off(
    db: AsyncSession, client: AsyncClient
) -> None:
    await _store(db, "security.require_uppercase", False)
    assert await _setup(db, client, NO_UPPERCASE) == 303
    assert await _users(db) == 1


async def test_user_create_honours_a_stored_require_uppercase_off(
    db: AsyncSession,
) -> None:
    await _store(db, "security.require_uppercase", False)
    created = await UserCoreService().create_user(
        db, UserCreate(username="relaxed", password=NO_UPPERCASE)
    )
    assert created.username == "relaxed"


async def test_reset_honours_a_stored_shorter_minimum(db: AsyncSession) -> None:
    await _store(db, "security.min_password_length", 6)
    user = make_user()
    db.add(user)
    await db.flush()
    await AuthCoreService().reset_password(db, user.id, "Ab3dEf7")
    assert AuthCoreService.verify_password("Ab3dEf7", user.password_hash)


async def test_change_password_honours_a_stored_require_uppercase_off(
    db: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    await _store(db, "security.require_uppercase", False)
    user = make_user()
    db.add(user)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, user.id)
    client.cookies.set(settings.security.session_cookie_name, token)

    resp = await client.post(
        "/change-password",
        data={
            "current_password": "TestPass123!",
            "new_password": NO_UPPERCASE,
            "confirm_password": NO_UPPERCASE,
        },
    )

    assert resp.status_code == 200, resp.text[:2000]
    assert "Password changed successfully" in resp.text
    await db.refresh(user)
    assert AuthCoreService.verify_password(NO_UPPERCASE, user.password_hash)
