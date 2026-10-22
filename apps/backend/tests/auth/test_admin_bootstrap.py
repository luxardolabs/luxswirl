"""The first admin and owner recovery (FLEET-ADMIN-BOOTSTRAP-STANDARD).

- Startup seeds the first admin from INITIAL_ADMIN_USERNAME / INITIAL_ADMIN_PASSWORD only
  while the users table is EMPTY, with must_change_password set. Before, the seed keyed on
  "no admin": an install whose admins were removed re-created one from an environment
  password that was still set.
- Otherwise /setup opens, gated by a one-time token printed in the server log. Before, the
  first visitor to /setup on a fresh, reachable install became admin.
- /setup is 404 once any user exists.
- `python -m app.cli reset-password <user>` gives a temporary password that must be changed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from app.core.rate_limit import limiter
from app.main import app
from app.models.session_model import Session
from app.models.user_model import User
from app.services.core import admin_bootstrap_core_service as bootstrap_module
from app.services.core.admin_bootstrap_core_service import (
    AdminBootstrapCoreService,
    BootstrapOutcome,
    UnknownUserError,
)
from app.services.core.auth_core_service import AuthCoreService
from fixtures.factories import make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

GOOD_PASSWORD = "A long enough passphrase 7"


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
            transport=ASGITransport(app=app), base_url="https://test"
        ) as c:
            yield c
    finally:
        limiter.reset()


def _on_disk(path: Path) -> str | None:
    """The setup token file's content, or None when there is none (sync: no event loop I/O)."""
    return path.read_text() if path.exists() else None


async def _users(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(User)) or 0)


# ── startup ────────────────────────────────────────────────────────────────────


async def test_startup_with_users_seeds_nothing_and_closes_setup(
    db: AsyncSession, token_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The owner renamed or removed the seeded admin; the env password is still set.
    db.add(make_user(username="owner", role="viewer"))
    await db.flush()
    monkeypatch.setenv("INITIAL_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", GOOD_PASSWORD)

    result = await AdminBootstrapCoreService().bootstrap(db)

    assert result.outcome is BootstrapOutcome.USERS_EXIST
    assert await _users(db) == 1
    assert _on_disk(token_file) is None


async def test_startup_seeds_the_first_admin_who_must_change_the_password(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INITIAL_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", GOOD_PASSWORD)

    result = await AdminBootstrapCoreService().bootstrap(db)

    assert result.outcome is BootstrapOutcome.SEEDED
    admin = await db.scalar(select(User).where(User.username == "admin"))
    assert admin is not None
    assert admin.role == "admin"
    assert admin.must_change_password is True
    assert AuthCoreService.verify_password(GOOD_PASSWORD, admin.password_hash)


async def test_a_rejected_seed_opens_setup_instead_of_crashing(
    db: AsyncSession, token_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INITIAL_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "short")

    result = await AdminBootstrapCoreService().bootstrap(db)

    assert result.outcome is BootstrapOutcome.SEED_REJECTED
    assert await _users(db) == 0
    assert result.setup_token and _on_disk(token_file) == result.setup_token


async def test_no_seed_opens_setup_with_a_token(
    db: AsyncSession, token_file: Path
) -> None:
    result = await AdminBootstrapCoreService().bootstrap(db)

    assert result.outcome is BootstrapOutcome.SETUP
    assert result.setup_token and _on_disk(token_file) == result.setup_token


# ── /login and /setup ─────────────────────────────────────────────────────────


async def test_login_sends_an_empty_install_to_setup(client: AsyncClient) -> None:
    r = await client.get("/login")

    assert r.status_code == 302
    assert r.headers["location"] == "/setup"


async def test_setup_without_the_logged_token_creates_nobody(
    client: AsyncClient, db: AsyncSession
) -> None:
    await AdminBootstrapCoreService().bootstrap(db)
    assert (await client.get("/setup")).status_code == 200

    r = await client.post(
        "/setup",
        data={
            "token": "guessed",
            "login": "mallory",
            "password": GOOD_PASSWORD,
            "password_confirm": GOOD_PASSWORD,
        },
    )

    assert r.status_code == 403
    assert await _users(db) == 0


async def test_setup_with_the_logged_token_creates_the_admin_and_closes(
    client: AsyncClient, db: AsyncSession, token_file: Path
) -> None:
    token = (await AdminBootstrapCoreService().bootstrap(db)).setup_token
    assert token

    r = await client.post(
        "/setup",
        data={
            "token": token,
            "login": "owner",
            "password": GOOD_PASSWORD,
            "password_confirm": GOOD_PASSWORD,
        },
    )

    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    admin = await db.scalar(select(User).where(User.username == "owner"))
    assert admin is not None and admin.role == "admin"
    assert admin.must_change_password is False
    # The token outlives the request only until its transaction commits (a failed commit leaves
    # /setup usable). The request's real get_db committed, so it is gone, and /setup is closed.
    assert _on_disk(token_file) is None
    assert (await client.get("/setup")).status_code == 404
    assert _on_disk(token_file) is None


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_setup_is_gone_once_any_user_exists(
    client: AsyncClient, db: AsyncSession, method: str
) -> None:
    db.add(make_user(role="viewer"))
    await db.flush()

    r = await client.request(
        method,
        "/setup",
        data={"token": "t", "login": "x", "password": "p", "password_confirm": "p"},
    )

    assert r.status_code == 404


async def test_login_renders_once_a_user_exists(
    client: AsyncClient, db: AsyncSession
) -> None:
    db.add(make_user(role="admin"))
    await db.flush()

    assert (await client.get("/login")).status_code == 200


# ── recovery ──────────────────────────────────────────────────────────────────


async def test_reset_password_gives_a_temporary_password_to_change(
    db: AsyncSession,
) -> None:
    user = make_user(username="owner")
    db.add(user)
    await db.flush()

    temporary = await AdminBootstrapCoreService().reset_password(db, "owner")

    await db.refresh(user)
    assert AuthCoreService.verify_password(temporary, user.password_hash)
    assert not AuthCoreService.verify_password("TestPass123!", user.password_hash)
    assert user.must_change_password is True


async def test_reset_password_ends_every_session_the_user_holds(
    db: AsyncSession,
) -> None:
    # An owner resets because someone else got in: that someone must not stay signed in.
    user, other = make_user(username="owner"), make_user(username="other")
    db.add_all([user, other])
    await db.flush()
    for holder in (user, user, other):
        await AuthCoreService().create_session(db, holder.id)

    await AdminBootstrapCoreService().reset_password(db, "owner")

    held = select(func.count()).select_from(Session).where(Session.user_id == user.id)
    kept = select(func.count()).select_from(Session).where(Session.user_id == other.id)
    assert await db.scalar(held) == 0
    assert await db.scalar(kept) == 1


async def test_setup_refuses_a_password_the_app_policy_refuses(
    client: AsyncClient, db: AsyncSession
) -> None:
    # Long enough for the bootstrap's own floor, but no uppercase or digit: the app's policy
    # (validate_password_complexity) refuses it, so the most privileged account cannot use it.
    token = (await AdminBootstrapCoreService().bootstrap(db)).setup_token
    weak = "a-long-enough-passphrase"

    r = await client.post(
        "/setup",
        data={
            "token": token,
            "login": "owner",
            "password": weak,
            "password_confirm": weak,
        },
    )

    assert r.status_code == 400
    assert await _users(db) == 0


async def test_reset_password_for_an_unknown_user_changes_nothing(
    db: AsyncSession,
) -> None:
    with pytest.raises(UnknownUserError):
        await AdminBootstrapCoreService().reset_password(db, "nobody")
