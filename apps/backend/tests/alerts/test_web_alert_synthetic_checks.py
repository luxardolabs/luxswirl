"""The web alert form attaches a synthetic check for an admin alone.

Every change to how a synthetic check is watched is an admin's, by any path:
the admin API refuses an editor's alert that names one, and so does the web
create form.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.rate_limit import limiter
from app.main import app
from app.models.alert_check_mapping_model import AlertCheckMapping
from app.models.alert_model import Alert
from app.models.alert_notification_mapping_model import AlertNotificationMapping
from app.services.core.auth_core_service import AuthCoreService
from fixtures.factories import (
    make_agent,
    make_check,
    make_notification_provider,
    make_user,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def _browser(db: AsyncSession, role: str) -> AsyncClient:
    user = make_user(role=role)
    db.add(user)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, user.id)
    client = AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    )
    client.cookies.set(settings.security.session_cookie_name, token)
    return client


@pytest_asyncio.fixture
async def s(db: AsyncSession, monkeypatch) -> AsyncIterator[dict]:
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    limiter.reset()
    agent = make_agent()
    db.add(agent)
    await db.flush()
    synthetic = make_check(
        agent_id=agent.id,
        check_type="synthetic",
        target="https://a.example",
        script_code="await page.goto(target)",
    )
    ping = make_check(agent_id=agent.id)
    db.add_all([synthetic, ping])
    await db.flush()
    clients = {role: await _browser(db, role) for role in ("editor", "admin")}
    yield {"synthetic": synthetic.id, "ping": ping.id, **clients}
    for client in clients.values():
        await client.aclose()
    limiter.reset()


def _form(name: str, *check_ids) -> dict[str, str]:
    form = {
        "name": name,
        "trigger_type": "status_change",
        "trigger_consecutive_failures": "3",
        "is_enabled": "true",
    }
    form.update({f"check_{c}": str(c) for c in check_ids})
    return form


async def _alerts_named(db: AsyncSession, name: str) -> int:
    return (
        await db.execute(select(func.count()).where(Alert.name == name))
    ).scalar_one()


async def _mappings(db: AsyncSession, check_id) -> int:
    return (
        await db.execute(
            select(func.count()).where(AlertCheckMapping.check_id == check_id)
        )
    ).scalar_one()


async def test_an_editor_cannot_create_an_alert_on_a_synthetic_check(
    db: AsyncSession, s: dict
) -> None:
    r = await s["editor"].post(
        "/alerts/create", data=_form("on-synthetic", s["synthetic"], s["ping"])
    )

    assert r.status_code == 403, (r.status_code, r.text[:300])
    assert await _alerts_named(db, "on-synthetic") == 0
    assert await _mappings(db, s["synthetic"]) == 0
    assert await _mappings(db, s["ping"]) == 0


async def test_an_editor_creates_an_alert_on_other_checks(
    db: AsyncSession, s: dict
) -> None:
    r = await s["editor"].post("/alerts/create", data=_form("on-ping", s["ping"]))

    assert r.status_code == 200, (r.status_code, r.text[:300])
    assert await _alerts_named(db, "on-ping") == 1
    assert await _mappings(db, s["ping"]) == 1


async def test_an_editors_global_alert_ignores_a_hidden_synthetic_box(
    db: AsyncSession, s: dict
) -> None:
    """The form hides, not disables, the check boxes under Global, so a box
    ticked under Specific is still posted; a global alert attaches none."""
    form = _form("global-stale", s["synthetic"])
    form["is_global"] = "true"

    r = await s["editor"].post("/alerts/create", data=form)

    assert r.status_code == 200, (r.status_code, r.text[:300])
    assert await _alerts_named(db, "global-stale") == 1
    assert await _mappings(db, s["synthetic"]) == 0


async def test_a_check_posted_twice_is_attached_once(db: AsyncSession, s: dict) -> None:
    form = _form("twice")
    form.update({"check_a": str(s["ping"]), "check_b": str(s["ping"])})

    r = await s["admin"].post("/alerts/create", data=form)

    assert r.status_code == 200, (r.status_code, r.text[:300])
    assert await _mappings(db, s["ping"]) == 1


async def test_a_provider_posted_twice_is_mapped_once(
    db: AsyncSession, s: dict
) -> None:
    provider = make_notification_provider()
    db.add(provider)
    await db.flush()
    form = _form("provider-twice", s["ping"])
    form.update({"provider_a": str(provider.id), "provider_b": str(provider.id)})

    r = await s["editor"].post("/alerts/create", data=form)

    assert r.status_code == 200, (r.status_code, r.text[:300])
    mapped = (
        await db.execute(
            select(func.count()).where(
                AlertNotificationMapping.notification_provider_id == provider.id
            )
        )
    ).scalar_one()
    assert mapped == 1


@pytest.mark.parametrize("field", ["check", "provider"])
async def test_an_unknown_id_is_refused_by_name(
    db: AsyncSession, s: dict, field: str
) -> None:
    unknown = uuid4()
    form = _form(f"unknown-{field}", s["ping"])
    form[f"{field}_x"] = str(unknown)

    r = await s["editor"].post("/alerts/create", data=form)

    assert r.status_code == 422, (r.status_code, r.text[:300])
    assert await _alerts_named(db, f"unknown-{field}") == 0


async def test_an_admin_creates_an_alert_on_a_synthetic_check(
    db: AsyncSession, s: dict
) -> None:
    r = await s["admin"].post(
        "/alerts/create", data=_form("admin-synthetic", s["synthetic"])
    )

    assert r.status_code == 200, (r.status_code, r.text[:300])
    assert await _alerts_named(db, "admin-synthetic") == 1
    assert await _mappings(db, s["synthetic"]) == 1
