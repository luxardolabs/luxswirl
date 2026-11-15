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


# bcrypt stops at a NUL, so "Ab1\0Ab1\0Ab1" (11 characters, upper, lower and
# digit) would be stored as a hash that "Ab1" also matches.
CYCLED = "Ab1\x00Ab1\x00Ab1"
CYCLED_PREFIX = "Ab1"


async def _signed_in(db: AsyncSession, client: AsyncClient) -> User:
    user = make_user()
    db.add(user)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, user.id)
    client.cookies.set(settings.security.session_cookie_name, token)
    return user


async def test_the_policy_refuses_a_password_with_a_nul(db: AsyncSession) -> None:
    valid, error = await AuthCoreService.validate_password_complexity(db, CYCLED)
    assert valid is False
    assert error == "Password contains a character that cannot be stored"


async def test_profile_change_password_with_nul_cannot_make_a_3_char_password(
    db: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    user = await _signed_in(db, client)
    resp = await client.post(
        "/profile/change-password",
        data={
            "current_password": "TestPass123!",
            "new_password": CYCLED,
            "confirm_password": CYCLED,
        },
    )
    assert resp.status_code < 500, resp.text[:500]
    assert "cannot be stored" in resp.text
    await db.refresh(user)
    assert not AuthCoreService.verify_password(CYCLED_PREFIX, user.password_hash)
    assert AuthCoreService.verify_password("TestPass123!", user.password_hash)


async def test_admin_reset_with_nul_cannot_set_a_3_char_password(
    db: AsyncSession,
) -> None:
    user = make_user()
    db.add(user)
    await db.flush()
    with pytest.raises(ValidationException, match="cannot be stored"):
        await AuthCoreService().reset_password(db, user.id, CYCLED)
    assert AuthCoreService.verify_password("TestPass123!", user.password_hash)


async def test_relaxed_policy_still_refuses_a_whitespace_only_password(
    db: AsyncSession,
) -> None:
    await _store(db, "security.min_password_length", 6)
    for key in ("require_uppercase", "require_lowercase", "require_number"):
        await _store(db, f"security.{key}", False)
    assert (await AuthCoreService.validate_password_complexity(db, "qwzxkv"))[0]
    for blank in ("      ", "\t\n    　 "):
        valid, _ = await AuthCoreService.validate_password_complexity(db, blank)
        assert valid is False
    with pytest.raises(ValidationException):
        await UserCoreService().create_user(
            db, UserCreate(username="blank-pw", password="      ")
        )


async def _relaxed(db: AsyncSession) -> None:
    await _store(db, "security.min_password_length", 6)
    for key in ("require_uppercase", "require_lowercase", "require_number"):
        await _store(db, f"security.{key}", False)


async def test_control_relaxed_policy_refuses_whitespace(db: AsyncSession) -> None:
    await _relaxed(db)
    assert (await AuthCoreService.validate_password_complexity(db, "qwzxkv"))[0]
    assert not (await AuthCoreService.validate_password_complexity(db, " " * 8))[0]


@pytest.mark.parametrize(
    "blank",
    [
        "\u200b" * 8,
        "\u3164" * 8,
        "\u2800" * 8,
        "\ufeff" * 8,
        "\u2060" * 8,
        "\u115f\u1160" * 4,
        "\uffa0" * 8,
        "\u200b \u3164\u2800\u00a0\u2028\u2029\x7f",
    ],
    ids=[
        "zwsp",
        "hangul-filler",
        "braille-blank",
        "bom",
        "word-joiner",
        "hangul-choseong-jungseong-fillers",
        "halfwidth-hangul-filler",
        "mixed",
    ],
)
async def test_relaxed_policy_refuses_an_invisible_only_password(
    db: AsyncSession, blank: str
) -> None:
    await _relaxed(db)
    valid, _ = await AuthCoreService.validate_password_complexity(db, blank)
    assert valid is False, f"invisible-only password {blank!r} accepted"


async def test_relaxed_policy_accepts_a_zwj_between_visible_characters(
    db: AsyncSession,
) -> None:
    """An emoji sequence joined by U+200D is a legitimate password."""
    await _relaxed(db)
    password = "qwz\U0001f469\u200d\U0001f4bbxkv"
    assert (await AuthCoreService.validate_password_complexity(db, password))[0]


async def test_the_change_password_page_lists_the_stored_policy(
    db: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    await _store(db, "security.min_password_length", 14)
    await _store(db, "security.require_uppercase", False)
    await _store(db, "security.require_special_char", True)
    await _signed_in(db, client)

    page = (await client.get("/change-password")).text

    assert "At least 14 characters" in page
    assert "At least one special character" in page
    assert "uppercase" not in page
    assert "Minimum 8 characters" not in page


async def test_the_user_create_form_lists_the_stored_policy(
    db: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    await _store(db, "security.min_password_length", 10)
    await _store(db, "security.require_number", False)
    await _signed_in(db, client)

    panel = (await client.get("/settings/users/create-form")).text

    assert "At least 10 characters" in panel
    assert "At least one uppercase letter" in panel
    assert "digit" not in panel
    assert "Min 8 characters" not in panel


async def test_the_profile_panel_lists_the_stored_policy(
    db: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    await _store(db, "security.min_password_length", 11)
    await _signed_in(db, client)

    panel = (await client.get("/profile")).text

    assert "At least 11 characters" in panel
