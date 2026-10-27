"""Settings on the admin API: an admin's alone.

A value is of the setting's stored type and meets its rules, or it is a 422.
Setting or resetting a setting applies what it governs (a TimescaleDB policy,
the display preferences) by either path. The metrics bearer token is
generated, never set; it is read masked and never logged.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.display_settings import current_display_settings, update_display_settings
from app.core.rate_limit import limiter
from app.main import app
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.api_token_core_service import ApiTokenCoreService
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.settings_core_service import SettingsCoreService
from app.services.core.timescale_core_service import TimescaleCoreService
from fixtures.factories import make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

SETTINGS = "/api/v1/admin/settings"


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


@pytest.fixture
def display() -> object:
    """Put the display preferences back as they were after the test."""
    before = current_display_settings()
    yield before
    update_display_settings(
        timezone=before.timezone,
        date_format=before.date_format,
        time_format=before.time_format,
    )


@pytest.fixture
def policies(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, int]]:
    """Every TimescaleDB policy change asked for, in order."""
    calls: list[tuple[str, str, int]] = []

    async def retention(_db: AsyncSession, relation: str, days: int) -> None:
        calls.append(("retention", relation, days))

    async def compression(_db: AsyncSession, relation: str, days: int) -> None:
        calls.append(("compression", relation, days))

    monkeypatch.setattr(
        TimescaleCoreService, "update_retention_policy", staticmethod(retention)
    )
    monkeypatch.setattr(
        TimescaleCoreService, "update_compression_policy", staticmethod(compression)
    )
    return calls


async def _as(db: AsyncSession, role: str) -> dict[str, str]:
    user = make_user(role=role)
    db.add(user)
    await db.flush()
    _, plaintext = await ApiTokenCoreService.create_token(
        db, user.id, ApiTokenCreate(name=role)
    )
    return {"Authorization": f"Bearer {plaintext}"}


# ── Who may ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["viewer", "editor"])
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "", None),
        ("GET", "/check.default_interval", None),
        ("PUT", "/check.default_interval", {"value": 30}),
        ("POST", "/check.default_interval/reset", None),
        ("POST", "/metrics-token", None),
    ],
)
async def test_only_an_admin_reaches_settings(
    db: AsyncSession,
    client: AsyncClient,
    role: str,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    other = await _as(db, role)

    resp = await client.request(method, SETTINGS + path, json=body, headers=other)

    assert resp.status_code == 403, resp.text
    assert await SettingsCoreService.get_setting(db, "check.default_interval") == 60
    assert await SettingsCoreService.get_setting(db, "metrics.bearer_token") == ""


async def test_no_token_is_refused(client: AsyncClient) -> None:
    assert (await client.get(SETTINGS)).status_code == 401


# ── Reads ───────────────────────────────────────────────────────────────────


async def test_settings_are_read_with_their_values_unwrapped(
    db: AsyncSession, client: AsyncClient
) -> None:
    admin = await _as(db, "admin")

    listed = await client.get(SETTINGS, headers=admin)
    database = await client.get(
        SETTINGS, params={"category": "database"}, headers=admin
    )
    unfiltered = await client.get(SETTINGS, params={"category": ""}, headers=admin)
    one = await client.get(f"{SETTINGS}/check.default_interval", headers=admin)

    assert listed.status_code == 200
    keys = [s["key"] for s in listed.json()]
    assert keys == sorted(keys) and "check.default_interval" in keys
    assert {s["category"] for s in database.json()} == {"database"}
    assert len(unfiltered.json()) == len(listed.json())
    assert one.json()["value"] == 60 and one.json()["type"] == "int"
    assert one.json()["validation"] == {"min": 10, "max": 86400}


async def test_an_unknown_key_is_404(db: AsyncSession, client: AsyncClient) -> None:
    admin = await _as(db, "admin")

    assert (await client.get(f"{SETTINGS}/no.such", headers=admin)).status_code == 404
    put = await client.put(f"{SETTINGS}/no.such", json={"value": 1}, headers=admin)
    assert put.status_code == 404
    reset = await client.post(f"{SETTINGS}/no.such/reset", headers=admin)
    assert reset.status_code == 404


# ── Setting values ──────────────────────────────────────────────────────────


async def test_a_value_of_the_setting_s_type_is_set(
    db: AsyncSession, client: AsyncClient
) -> None:
    admin = await _as(db, "admin")

    resp = await client.put(
        f"{SETTINGS}/check.default_interval", json={"value": 120}, headers=admin
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["value"] == 120
    assert await SettingsCoreService.get_setting(db, "check.default_interval") == 120


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("check.default_interval", "120"),
        ("check.default_interval", 120.5),
        ("check.default_interval", True),
        ("check.default_interval", None),
        ("check.default_interval", [120]),
        ("check.default_interval", 5),
        ("check.default_verify_ssl", 1),
        ("check.default_http_method", "TRACE"),
        ("general.timezone", 5),
        ("general.default_page_size", 30),
        ("security.login_rate_limit", 10),
    ],
)
async def test_a_value_that_does_not_fit_is_a_422(
    db: AsyncSession, client: AsyncClient, key: str, value: object
) -> None:
    admin = await _as(db, "admin")
    before = await SettingsCoreService.get_setting(db, key)

    resp = await client.put(f"{SETTINGS}/{key}", json={"value": value}, headers=admin)

    assert resp.status_code == 422, resp.text
    assert await SettingsCoreService.get_setting(db, key) == before


async def test_a_reset_restores_the_default(
    db: AsyncSession, client: AsyncClient
) -> None:
    admin = await _as(db, "admin")
    await client.put(
        f"{SETTINGS}/check.default_interval", json={"value": 120}, headers=admin
    )

    resp = await client.post(f"{SETTINGS}/check.default_interval/reset", headers=admin)

    assert resp.status_code == 200, resp.text
    assert resp.json()["value"] == 60


# ── What a setting governs ──────────────────────────────────────────────────


async def test_a_retention_setting_moves_its_policy_when_set_and_reset(
    db: AsyncSession,
    client: AsyncClient,
    policies: list[tuple[str, str, int]],
) -> None:
    admin = await _as(db, "admin")

    await client.put(
        f"{SETTINGS}/database.retention_days", json={"value": 30}, headers=admin
    )
    await client.put(
        f"{SETTINGS}/database.compress_after_days", json={"value": 3}, headers=admin
    )
    await client.post(f"{SETTINGS}/database.retention_days/reset", headers=admin)

    assert policies == [
        ("retention", "check_results", 30),
        ("compression", "check_results", 3),
        ("retention", "check_results", 90),
    ]


async def test_a_web_reset_of_a_retention_setting_moves_its_policy(
    db: AsyncSession, client: AsyncClient, policies: list[tuple[str, str, int]]
) -> None:
    user = make_user(role="admin")
    db.add(user)
    await db.flush()
    _, session_token = await AuthCoreService().create_session(db, user.id)
    await SettingsCoreService.update_setting(db, "database.retention_days", 30)
    client.cookies.set(settings.security.session_cookie_name, session_token)

    resp = await client.post("/settings/database.retention_days/reset")

    assert resp.status_code == 200, resp.text
    assert policies == [("retention", "check_results", 90)]


async def test_a_display_setting_changes_what_is_shown_when_set_and_reset(
    db: AsyncSession, client: AsyncClient, display: object
) -> None:
    admin = await _as(db, "admin")

    await client.put(
        f"{SETTINGS}/general.timezone", json={"value": "Asia/Tokyo"}, headers=admin
    )
    set_to = current_display_settings().timezone
    await client.post(f"{SETTINGS}/general.timezone/reset", headers=admin)

    assert set_to == "Asia/Tokyo"
    assert current_display_settings().timezone == "UTC"


# ── The metrics bearer token ────────────────────────────────────────────────


async def test_the_metrics_token_is_generated_shown_once_and_read_masked(
    db: AsyncSession, client: AsyncClient, app_logs: list[logging.LogRecord]
) -> None:
    admin = await _as(db, "admin")

    created = await client.post(f"{SETTINGS}/metrics-token", headers=admin)

    assert created.status_code == 201, created.text
    token = created.json()["token"]
    assert token.startswith("luxswirl_metrics_")
    assert await SettingsCoreService.get_setting(db, "metrics.bearer_token") == token
    read = await client.get(f"{SETTINGS}/metrics.bearer_token", headers=admin)
    listed = await client.get(SETTINGS, headers=admin)
    assert token not in read.text and token not in listed.text
    assert read.json()["value"] == "********" and read.json()["generated"] is True
    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in app_logs)
    assert token not in logged


async def test_the_metrics_token_is_not_set_to_a_chosen_value(
    db: AsyncSession, client: AsyncClient
) -> None:
    admin = await _as(db, "admin")

    resp = await client.put(
        f"{SETTINGS}/metrics.bearer_token", json={"value": "chosen"}, headers=admin
    )

    assert resp.status_code == 422, resp.text
    assert await SettingsCoreService.get_setting(db, "metrics.bearer_token") == ""


async def test_an_unset_metrics_token_reads_empty(
    db: AsyncSession, client: AsyncClient
) -> None:
    admin = await _as(db, "admin")

    read = await client.get(f"{SETTINGS}/metrics.bearer_token", headers=admin)

    assert read.json()["value"] == ""
