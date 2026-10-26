"""What the admin API holds an alert to, past the shape of its body.

- A snooze, however often repeated, ends within a day.
- A trigger_config fits its trigger type, or evaluation would raise on it for
  every alert on the check.
- An alert's synthetic checks are attached and detached by an admin alone.
- A provider deleted elsewhere drops out of an alert's providers.
- Whether an alert is global is fixed when it is created.
- A field sent as null is cleared; a required one cannot be.
- Two replacements of an alert's providers at once leave one mapping, not a
  database error.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.rate_limit import limiter
from app.crud.alert_crud import AlertCRUD
from app.main import app
from app.models.alert_notification_mapping_model import AlertNotificationMapping
from app.models.notification_provider_model import NotificationProvider
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.alert_core_service import AlertCoreService
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
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

ALERTS = "/api/v1/admin/alerts"


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
        db, user.id, ApiTokenCreate(name=role)
    )
    return {"Authorization": f"Bearer {plaintext}"}


async def _check(db: AsyncSession, **overrides) -> UUID:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(agent_id=agent.id, **overrides)
    db.add(check)
    await db.flush()
    return check.id


async def _synthetic(db: AsyncSession) -> UUID:
    return await _check(
        db,
        check_type="synthetic",
        target="https://a.example",
        script_code="await page.goto('https://a.example')",
    )


async def _alert(db: AsyncSession, **overrides) -> UUID:
    alert = make_alert(**overrides)
    db.add(alert)
    await db.flush()
    return alert.id


# ── Snooze ───────────────────────────────────────────────────────────────────


async def test_a_snooze_ends_within_a_day_however_often_repeated(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _as(db, "viewer")
    check = await _check(db)
    alert_id = await _alert(db)
    db.add(make_alert_check_mapping(alert_id=alert_id, check_id=check))
    await db.flush()
    url = f"{ALERTS}/{alert_id}/checks/{check}/snooze"

    for _ in range(5):
        last = await client.post(url, json={"minutes": 1440}, headers=viewer)

    until = datetime.fromisoformat(last.json()["snoozed_until"])
    assert until <= utc_now() + timedelta(days=1, minutes=1)


# ── trigger_config fits its trigger ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("trigger_type", "config"),
    [
        ("status_change", {"consecutive_failures": "3"}),
        ("status_change", {"consecutive_failures": 0}),
        ("status_change", {"on_status": ["broken"]}),
        ("status_change", {"surprise": 1}),
        ("threshold", {"operator": ">"}),
        ("threshold", {"value": "1000"}),
        ("threshold", {"value": 5, "operator": "~"}),
        ("ssl_cert_expiry", {"days_thresholds": ["30"]}),
        ("ssl_cert_expiry", {"days_thresholds": [0]}),
        ("no_data", {"missed_intervals": "3"}),
        ("agent_offline", {"missed_intervals": 0}),
        ("status_change", {"check_filters": {"tags": "prod"}}),
    ],
)
async def test_a_config_that_does_not_fit_its_trigger_is_refused(
    db: AsyncSession, client: AsyncClient, trigger_type: str, config: dict
) -> None:
    editor = await _as(db, "editor")

    resp = await client.post(
        ALERTS,
        json={"name": "bad", "trigger_type": trigger_type, "trigger_config": config},
        headers=editor,
    )

    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize(
    ("trigger_type", "config", "stored"),
    [
        (
            "status_change",
            {"consecutive_failures": 3},
            {"on_status": ["error"], "consecutive_failures": 3},
        ),
        (
            "threshold",
            {"value": 1500},
            {"metric": "latency_ms", "operator": ">", "value": 1500.0},
        ),
        ("ssl_cert_expiry", {"days_thresholds": [7, 30]}, {"days_thresholds": [7, 30]}),
        ("no_data", {"missed_intervals": 5}, {"missed_intervals": 5}),
    ],
)
async def test_a_config_is_stored_in_the_form_evaluation_reads(
    db: AsyncSession,
    client: AsyncClient,
    trigger_type: str,
    config: dict,
    stored: dict,
) -> None:
    editor = await _as(db, "editor")

    resp = await client.post(
        ALERTS,
        json={
            "name": f"ok-{uuid4().hex[:6]}",
            "trigger_type": trigger_type,
            "trigger_config": config,
        },
        headers=editor,
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["trigger_config"] == stored


async def test_an_edited_config_is_held_to_the_alerts_trigger(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    alert_id = await _alert(db)  # status_change

    alone = await client.patch(
        f"{ALERTS}/{alert_id}",
        json={"trigger_config": {"consecutive_failures": "2"}},
        headers=editor,
    )
    type_without_config = await client.patch(
        f"{ALERTS}/{alert_id}", json={"trigger_type": "threshold"}, headers=editor
    )
    both = await client.patch(
        f"{ALERTS}/{alert_id}",
        json={"trigger_type": "threshold", "trigger_config": {"value": 900}},
        headers=editor,
    )

    assert (alone.status_code, type_without_config.status_code) == (422, 422)
    assert both.status_code == 200, both.text
    assert both.json()["trigger_config"]["value"] == 900.0


# ── Synthetic checks ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["editor", "admin"])
async def test_only_an_admin_attaches_or_detaches_a_synthetic_check(
    db: AsyncSession, client: AsyncClient, role: str
) -> None:
    token = await _as(db, role)
    synthetic = await _synthetic(db)
    plain = await _check(db)
    alert_id = await _alert(db)
    db.add(make_alert_check_mapping(alert_id=alert_id, check_id=synthetic))
    await db.flush()

    detach = await client.put(
        f"{ALERTS}/{alert_id}/checks", json={"check_ids": []}, headers=token
    )
    create = await client.post(
        ALERTS,
        json={
            "name": f"s-{uuid4().hex[:6]}",
            "trigger_type": "status_change",
            "trigger_config": {},
            "check_ids": [str(synthetic)],
        },
        headers=token,
    )

    expected = 200 if role == "admin" else 403
    assert detach.status_code == expected, detach.text
    assert create.status_code == (201 if role == "admin" else 403), create.text
    if role == "editor":
        kept = await client.put(
            f"{ALERTS}/{alert_id}/checks",
            json={"check_ids": [str(synthetic), str(plain)]},
            headers=token,
        )
        assert kept.status_code == 200, kept.text
        assert kept.json() == {"added": 1, "removed": 0}


# ── Providers deleted elsewhere ──────────────────────────────────────────────


async def test_a_deleted_provider_drops_out_and_the_list_round_trips(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    live = make_notification_provider()
    gone = make_notification_provider()
    db.add_all([live, gone])
    await db.flush()
    live_id, gone_id = live.id, gone.id
    created = await client.post(
        ALERTS,
        json={
            "name": f"r-{uuid4().hex[:6]}",
            "trigger_type": "status_change",
            "trigger_config": {},
            "notification_provider_ids": [str(live_id), str(gone_id)],
        },
        headers=editor,
    )
    alert_id = created.json()["id"]
    await db.execute(
        update(NotificationProvider)
        .where(NotificationProvider.id == gone_id)
        .values(deleted_at=utc_now())
    )

    read = (await client.get(f"{ALERTS}/{alert_id}", headers=editor)).json()
    again = await client.put(
        f"{ALERTS}/{alert_id}/providers",
        json={"notification_provider_ids": read["notification_provider_ids"]},
        headers=editor,
    )

    assert read["notification_provider_ids"] == [str(live_id)]
    assert again.status_code == 200, again.text


# ── Edits ────────────────────────────────────────────────────────────────────


async def test_an_empty_filter_reads_as_all(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _as(db, "viewer")
    off = await _alert(db, is_enabled=False)

    resp = await client.get(
        ALERTS,
        params={"is_enabled": "", "is_global": "", "per_page": 200},
        headers=viewer,
    )

    assert resp.status_code == 200, resp.text
    assert str(off) in {a["id"] for a in resp.json()["items"]}


async def test_whether_an_alert_is_global_is_fixed(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    alert_id = await _alert(db)

    resp = await client.patch(
        f"{ALERTS}/{alert_id}", json={"is_global": True}, headers=editor
    )

    assert resp.status_code == 422, resp.text


async def test_null_clears_a_field_but_never_a_required_one(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    alert_id = await _alert(
        db, resend_interval_minutes=30, max_resends=5, description="d"
    )

    cleared = await client.patch(
        f"{ALERTS}/{alert_id}",
        json={
            "resend_interval_minutes": None,
            "max_resends": None,
            "description": None,
        },
        headers=editor,
    )
    required = await client.patch(
        f"{ALERTS}/{alert_id}", json={"name": None}, headers=editor
    )

    body = cleared.json()
    assert (
        body["resend_interval_minutes"],
        body["max_resends"],
        body["description"],
    ) == (
        None,
        None,
        None,
    )
    assert required.status_code == 422, required.text


# ── Concurrent replacements ──────────────────────────────────────────────────


async def test_two_replacements_at_once_leave_one_mapping(
    worker_db, monkeypatch
) -> None:
    async with worker_db() as s:
        provider = make_notification_provider()
        alert = make_alert()
        s.add_all([provider, alert])
        await s.commit()
        provider_id, alert_id = provider.id, alert.id
    original = AlertCRUD.list_mapped_provider_ids
    raced = {"done": False}

    async def read_then_race(db, aid):
        rows = await original(db, aid)
        if not raced["done"]:
            raced["done"] = True
            async with worker_db() as other:
                await AlertCoreService.set_providers(other, alert_id, [provider_id])
                await other.commit()
        return rows

    monkeypatch.setattr(
        AlertCRUD, "list_mapped_provider_ids", staticmethod(read_then_race)
    )
    async with worker_db() as s:
        await AlertCoreService.set_providers(s, alert_id, [provider_id])
        await s.commit()

    async with worker_db() as s:
        count = await s.scalar(
            select(func.count())
            .select_from(AlertNotificationMapping)
            .where(AlertNotificationMapping.alert_id == alert_id)
        )
    assert count == 1
