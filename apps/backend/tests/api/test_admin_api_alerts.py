"""Alert rules on the admin API: read by any token, changed by an editor.

An alert is read with the providers it notifies and the checks it watches;
both are replaced whole, keeping what stays (a kept check keeps its snooze).
An unknown provider or check is refused by name, never a database error.
Any token may snooze an alert for one check, as any signed-in user may in the
web UI.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.rate_limit import limiter
from app.main import app
from app.models.alert_check_mapping_model import AlertCheckMapping
from app.models.alert_model import Alert
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.api_token_core_service import ApiTokenCoreService
from fixtures.factories import (
    make_agent,
    make_alert,
    make_alert_check_mapping,
    make_check,
    make_notification_provider,
    make_user,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

ALERTS = "/api/v1/admin/alerts"
TRIGGER = {
    "trigger_type": "status_change",
    "trigger_config": {"on_status": ["error"], "consecutive_failures": 2},
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


async def _as(db: AsyncSession, role: str) -> dict[str, str]:
    user = make_user(role=role)
    db.add(user)
    await db.flush()
    _, plaintext = await ApiTokenCoreService.create_token(
        db, user.id, ApiTokenCreate(name=role, scopes=["*"])
    )
    return {"Authorization": f"Bearer {plaintext}"}


async def _provider(db: AsyncSession) -> UUID:
    provider = make_notification_provider()
    db.add(provider)
    await db.flush()
    return provider.id


async def _checks(db: AsyncSession, n: int) -> list[UUID]:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    checks = [make_check(agent_id=agent.id) for _ in range(n)]
    db.add_all(checks)
    await db.flush()
    return [c.id for c in checks]


async def _alert(db: AsyncSession, **overrides) -> UUID:
    alert = make_alert(**overrides)
    db.add(alert)
    await db.flush()
    return alert.id


async def _alerts_in_db(db: AsyncSession) -> int:
    return int(
        await db.scalar(
            select(func.count()).select_from(Alert).where(Alert.deleted_at.is_(None))
        )
        or 0
    )


# ── Roles ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "", {"name": "x", **TRIGGER}),
        ("PATCH", "/{id}", {"name": "y"}),
        ("DELETE", "/{id}", None),
        ("POST", "/{id}/enable", None),
        ("POST", "/{id}/disable", None),
        ("PUT", "/{id}/providers", {"notification_provider_ids": []}),
        ("PUT", "/{id}/checks", {"check_ids": []}),
    ],
)
async def test_a_viewer_cannot_change_an_alert(
    db: AsyncSession,
    client: AsyncClient,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    viewer = await _as(db, "viewer")
    alert_id = await _alert(db, name="unchanged")

    resp = await client.request(
        method, ALERTS + path.format(id=alert_id), json=body, headers=viewer
    )

    assert resp.status_code == 403, resp.text
    after = (await client.get(f"{ALERTS}/{alert_id}", headers=viewer)).json()
    assert (after["name"], after["is_enabled"]) == ("unchanged", True)


async def test_no_token_is_refused(client: AsyncClient) -> None:
    assert (await client.get(ALERTS)).status_code == 401


# ── Create, read, change ─────────────────────────────────────────────────────


async def test_an_editor_creates_an_alert_read_back_with_its_mappings(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    provider = await _provider(db)
    checks = await _checks(db, 2)

    created = await client.post(
        ALERTS,
        json={
            "name": f"down-{uuid4().hex[:6]}",
            **TRIGGER,
            "notification_provider_ids": [str(provider), str(provider)],
            "check_ids": [str(c) for c in checks],
        },
        headers=editor,
    )

    assert created.status_code == 201, created.text
    body = created.json()
    assert body["notification_provider_ids"] == [str(provider)]
    assert sorted(body["check_ids"]) == sorted(str(c) for c in checks)
    assert (body["notification_provider_count"], body["check_count"]) == (1, 2)


@pytest.mark.parametrize("field", ["notification_provider_ids", "check_ids"])
async def test_an_unknown_provider_or_check_is_refused_by_name(
    db: AsyncSession, client: AsyncClient, field: str
) -> None:
    editor = await _as(db, "editor")
    before = await _alerts_in_db(db)
    missing = uuid4()

    resp = await client.post(
        ALERTS,
        json={"name": "ghost", **TRIGGER, field: [str(missing)]},
        headers=editor,
    )

    assert resp.status_code == 422, resp.text
    assert str(missing) in resp.text
    assert await _alerts_in_db(db) == before


@pytest.mark.parametrize(
    "body",
    [
        {"name": "g", **TRIGGER, "is_global": True, "check_ids": ["CHECK"]},
        {"name": "x", **TRIGGER, "deleted_at": "2026-01-01T00:00:00Z"},
        {"name": "x", "trigger_type": "nonsense", "trigger_config": {}},
        {"name": "", **TRIGGER},
    ],
)
async def test_a_bad_create_is_refused(
    db: AsyncSession, client: AsyncClient, body: dict[str, object]
) -> None:
    editor = await _as(db, "editor")
    check = (await _checks(db, 1))[0]
    payload = {k: ([str(check)] if v == ["CHECK"] else v) for k, v in body.items()}

    resp = await client.post(ALERTS, json=payload, headers=editor)

    assert resp.status_code == 422, resp.text


async def test_an_alert_is_edited_disabled_and_enabled(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    alert_id = await _alert(db)

    edited = await client.patch(
        f"{ALERTS}/{alert_id}",
        json={"name": "renamed", "resend_interval_minutes": 30},
        headers=editor,
    )
    off = await client.post(f"{ALERTS}/{alert_id}/disable", headers=editor)
    on = await client.post(f"{ALERTS}/{alert_id}/enable", headers=editor)

    assert edited.status_code == 200, edited.text
    assert (edited.json()["name"], edited.json()["resend_interval_minutes"]) == (
        "renamed",
        30,
    )
    assert off.json()["is_enabled"] is False
    assert on.json()["is_enabled"] is True


async def test_an_edit_refuses_a_field_it_does_not_take(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    alert_id = await _alert(db)

    resp = await client.patch(
        f"{ALERTS}/{alert_id}", json={"check_ids": []}, headers=editor
    )

    assert resp.status_code == 422, resp.text


async def test_a_deleted_alert_is_gone_from_the_api(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    alert_id = await _alert(db)

    gone = await client.delete(f"{ALERTS}/{alert_id}", headers=editor)
    read = await client.get(f"{ALERTS}/{alert_id}", headers=editor)
    again = await client.patch(
        f"{ALERTS}/{alert_id}", json={"name": "zombie"}, headers=editor
    )

    assert gone.status_code == 204
    assert (read.status_code, again.status_code) == (404, 404)


async def test_the_list_filters_by_enabled(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _as(db, "viewer")
    on = await _alert(db, is_enabled=True)
    off = await _alert(db, is_enabled=False)

    disabled = await client.get(
        ALERTS, params={"is_enabled": False, "per_page": 200}, headers=viewer
    )

    ids = {a["id"] for a in disabled.json()["items"]}
    assert str(off) in ids and str(on) not in ids


# ── Replacing providers and checks ───────────────────────────────────────────


async def test_providers_are_replaced_whole(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    keep, drop, add = await _provider(db), await _provider(db), await _provider(db)
    created = await client.post(
        ALERTS,
        json={
            "name": f"p-{uuid4().hex[:6]}",
            **TRIGGER,
            "notification_provider_ids": [str(keep), str(drop)],
        },
        headers=editor,
    )
    alert_id = created.json()["id"]

    change = await client.put(
        f"{ALERTS}/{alert_id}/providers",
        json={"notification_provider_ids": [str(keep), str(add)]},
        headers=editor,
    )

    assert change.json() == {"added": 1, "removed": 1}
    after = (await client.get(f"{ALERTS}/{alert_id}", headers=editor)).json()
    assert sorted(after["notification_provider_ids"]) == sorted([str(keep), str(add)])


async def test_a_kept_check_keeps_its_snooze(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    keep, drop, add = await _checks(db, 3)
    alert_id = await _alert(db)
    until = utc_now() + timedelta(hours=2)
    db.add(
        make_alert_check_mapping(alert_id=alert_id, check_id=keep, snoozed_until=until)
    )
    db.add(make_alert_check_mapping(alert_id=alert_id, check_id=drop))
    await db.flush()

    change = await client.put(
        f"{ALERTS}/{alert_id}/checks",
        json={"check_ids": [str(keep), str(add)]},
        headers=editor,
    )

    assert change.json() == {"added": 1, "removed": 1}
    db.expire_all()
    snoozed = await db.scalar(
        select(AlertCheckMapping.snoozed_until).where(
            AlertCheckMapping.alert_id == alert_id, AlertCheckMapping.check_id == keep
        )
    )
    assert snoozed is not None and abs((snoozed - until).total_seconds()) < 1


async def test_a_global_alert_takes_no_check_list(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    alert_id = await _alert(db, is_global=True)
    (check,) = await _checks(db, 1)

    resp = await client.put(
        f"{ALERTS}/{alert_id}/checks", json={"check_ids": [str(check)]}, headers=editor
    )

    assert resp.status_code == 422, resp.text


async def test_a_replacement_with_an_unknown_provider_changes_nothing(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    provider = await _provider(db)
    created = await client.post(
        ALERTS,
        json={
            "name": f"q-{uuid4().hex[:6]}",
            **TRIGGER,
            "notification_provider_ids": [str(provider)],
        },
        headers=editor,
    )
    alert_id = created.json()["id"]

    resp = await client.put(
        f"{ALERTS}/{alert_id}/providers",
        json={"notification_provider_ids": [str(uuid4())]},
        headers=editor,
    )

    assert resp.status_code == 422, resp.text
    after = (await client.get(f"{ALERTS}/{alert_id}", headers=editor)).json()
    assert after["notification_provider_ids"] == [str(provider)]


# ── Snooze ───────────────────────────────────────────────────────────────────


async def test_any_token_snoozes_and_unsnoozes(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _as(db, "viewer")
    (check,) = await _checks(db, 1)
    alert_id = await _alert(db)
    db.add(make_alert_check_mapping(alert_id=alert_id, check_id=check))
    await db.flush()
    url = f"{ALERTS}/{alert_id}/checks/{check}/snooze"

    first = await client.post(url, json={"minutes": 30}, headers=viewer)
    extended = await client.post(url, headers=viewer)
    cleared = await client.delete(url, headers=viewer)

    assert first.status_code == 200, first.text
    t1 = first.json()["snoozed_until"]
    t2 = extended.json()["snoozed_until"]
    assert t2 > t1
    assert cleared.status_code == 204


@pytest.mark.parametrize("minutes", [0, 1441])
async def test_a_snooze_outside_a_day_is_refused(
    db: AsyncSession, client: AsyncClient, minutes: int
) -> None:
    viewer = await _as(db, "viewer")
    (check,) = await _checks(db, 1)
    alert_id = await _alert(db)
    db.add(make_alert_check_mapping(alert_id=alert_id, check_id=check))
    await db.flush()

    resp = await client.post(
        f"{ALERTS}/{alert_id}/checks/{check}/snooze",
        json={"minutes": minutes},
        headers=viewer,
    )

    assert resp.status_code == 422


async def test_a_snooze_of_an_unmapped_check_is_404(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _as(db, "viewer")
    (check,) = await _checks(db, 1)
    alert_id = await _alert(db)

    resp = await client.post(
        f"{ALERTS}/{alert_id}/checks/{check}/snooze", headers=viewer
    )

    assert resp.status_code == 404
