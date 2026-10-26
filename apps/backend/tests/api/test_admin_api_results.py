"""A check's results, one result, history and summary on the admin API.

Any token reads them. A viewer's token reads every error with its credentials
masked and never a result's response body; an editor's reads them as stored.
A result is read only through its own check.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.rate_limit import limiter
from app.main import app
from app.models.check_artifact_model import CheckArtifact
from app.models.enum_model import ArtifactContentType, CheckArtifactType
from app.schemas.api_token_schema import ApiTokenCreate
from app.services.core.api_token_core_service import ApiTokenCoreService
from fixtures.factories import make_agent, make_check, make_check_result, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

CHECKS = "/api/v1/admin/checks"
DSN_ERROR = "connect to postgresql://monitor:s3cret-pw@db.internal:5432/app failed"
BODY = '{"session": "tok-abc123"}'


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


async def _check(db: AsyncSession):
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(agent_id=agent.id)
    db.add(check)
    await db.flush()
    return check


async def _result(db: AsyncSession, check, minutes_ago: float, **overrides):
    result = make_check_result(
        check_id=check.id,
        agent_id=check.agent_id,
        timestamp=utc_now() - timedelta(minutes=minutes_ago),
        **overrides,
    )
    db.add(result)
    await db.flush()
    return result


async def test_no_token_is_refused(client: AsyncClient) -> None:
    resp = await client.get(f"{CHECKS}/{uuid4()}/results")

    assert resp.status_code == 401


async def test_results_are_paged_newest_first_over_the_last_day(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _as(db, "viewer")
    check = await _check(db)
    for minutes in (5, 10, 15):
        await _result(db, check, minutes)
    await _result(db, check, 60 * 25)  # outside the default 24 hours

    first = await client.get(
        f"{CHECKS}/{check.id}/results", params={"per_page": 2}, headers=viewer
    )
    second = await client.get(
        f"{CHECKS}/{check.id}/results",
        params={"per_page": 2, "page": 2},
        headers=viewer,
    )

    assert first.status_code == 200, first.text
    body = first.json()
    assert (body["total"], body["has_next"]) == (3, True)
    stamps = [r["timestamp"] for r in body["items"] + second.json()["items"]]
    assert stamps == sorted(stamps, reverse=True)
    assert len(stamps) == 3


async def test_since_and_until_bound_the_window(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _as(db, "viewer")
    check = await _check(db)
    old = await _result(db, check, 60 * 30)
    await _result(db, check, 5)
    since = (utc_now() - timedelta(hours=31)).isoformat()
    until = (utc_now() - timedelta(hours=29)).isoformat()

    resp = await client.get(
        f"{CHECKS}/{check.id}/results",
        params={"since": since, "until": until},
        headers=viewer,
    )

    assert [r["id"] for r in resp.json()["items"]] == [str(old.id)]


@pytest.mark.parametrize(
    ("since_hours", "until_hours"),
    [(1, 2), (24 * 32, 0), (5, 5)],
    ids=["backwards", "over-31-days", "empty"],
)
async def test_a_bad_window_is_refused(
    db: AsyncSession, client: AsyncClient, since_hours: int, until_hours: int
) -> None:
    viewer = await _as(db, "viewer")
    check = await _check(db)
    now = utc_now()

    resp = await client.get(
        f"{CHECKS}/{check.id}/results",
        params={
            "since": (now - timedelta(hours=since_hours)).isoformat(),
            "until": (now - timedelta(hours=until_hours)).isoformat(),
        },
        headers=viewer,
    )

    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize("path", ["results", "history", "summary"])
async def test_an_unknown_check_is_404(
    db: AsyncSession, client: AsyncClient, path: str
) -> None:
    viewer = await _as(db, "viewer")

    resp = await client.get(f"{CHECKS}/{uuid4()}/{path}", headers=viewer)

    assert resp.status_code == 404


async def test_one_result_whole_with_its_artifacts(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    check = await _check(db)
    result = await _result(
        db,
        check,
        3,
        success=False,
        error=DSN_ERROR,
        response_data=BODY,
        metrics=json.dumps({"dns": {"ttl": 300}}),
    )
    db.add(
        CheckArtifact(
            check_id=check.id,
            check_result_id=result.id,
            check_result_timestamp=result.timestamp,
            artifact_type=CheckArtifactType.SCREENSHOT,
            content_type=ArtifactContentType.PNG,
            filename="shot.png",
            size_bytes=4,
            data=b"\x89PNG",
        )
    )
    await db.flush()

    resp = await client.get(f"{CHECKS}/{check.id}/results/{result.id}", headers=editor)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["error"], body["response_data"]) == (DSN_ERROR, BODY)
    assert body["metrics"] == {"dns": {"ttl": 300}}
    assert [a["filename"] for a in body["artifacts"]] == ["shot.png"]
    assert "data" not in body["artifacts"][0]


async def test_a_viewer_reads_no_credential_and_no_response_body(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _as(db, "viewer")
    check = await _check(db)
    result = await _result(
        db, check, 3, success=False, error=DSN_ERROR, response_data=BODY
    )

    one = await client.get(f"{CHECKS}/{check.id}/results/{result.id}", headers=viewer)
    page = await client.get(f"{CHECKS}/{check.id}/results", headers=viewer)
    history = await client.get(f"{CHECKS}/{check.id}/history", headers=viewer)
    summary = await client.get(f"{CHECKS}/{check.id}/summary", headers=viewer)

    for resp in (one, page, history, summary):
        assert resp.status_code == 200, resp.text
        assert "s3cret-pw" not in resp.text
        assert "tok-abc123" not in resp.text
    assert one.json()["response_data"] is None
    assert one.json()["error"] == (
        "connect to postgresql://***@db.internal:5432/app failed"
    )


async def test_a_result_of_another_check_is_not_found(
    db: AsyncSession, client: AsyncClient
) -> None:
    editor = await _as(db, "editor")
    mine = await _check(db)
    theirs = await _check(db)
    their_result = await _result(db, theirs, 3)

    resp = await client.get(
        f"{CHECKS}/{mine.id}/results/{their_result.id}", headers=editor
    )
    missing = await client.get(f"{CHECKS}/{mine.id}/results/{uuid4()}", headers=editor)

    assert (resp.status_code, missing.status_code) == (404, 404)


async def test_the_summary_counts_the_window_and_names_the_latest(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _as(db, "viewer")
    check = await _check(db)
    await _result(db, check, 30, success=True, latency_ms=10.0)
    await _result(db, check, 20, success=True, latency_ms=30.0)
    await _result(db, check, 10, success=False, latency_ms=20.0)
    latest = await _result(db, check, 1, success=False, latency_ms=None)
    await _result(db, check, 60 * 3, success=True)  # outside hours=2

    resp = await client.get(
        f"{CHECKS}/{check.id}/summary", params={"hours": 2}, headers=viewer
    )

    body = resp.json()
    assert (body["total"], body["successful"]) == (4, 2)
    assert body["uptime_percent"] == 50.0
    assert (body["min_latency_ms"], body["max_latency_ms"]) == (10.0, 30.0)
    assert body["avg_latency_ms"] == pytest.approx(20.0)
    assert body["latest"]["id"] == str(latest.id)


async def test_the_summary_of_a_check_with_no_results(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _as(db, "viewer")
    check = await _check(db)

    body = (await client.get(f"{CHECKS}/{check.id}/summary", headers=viewer)).json()

    assert (body["total"], body["uptime_percent"], body["latest"]) == (0, None, None)


async def test_the_history_is_bucketed_over_the_hours_asked(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _as(db, "viewer")
    check = await _check(db)
    await _result(db, check, 10, success=True, latency_ms=10.0)
    await _result(db, check, 10.2, success=False, latency_ms=30.0)
    await _result(db, check, 60 * 5, success=True)  # outside hours=4

    resp = await client.get(
        f"{CHECKS}/{check.id}/history", params={"hours": 4}, headers=viewer
    )

    body = resp.json()
    assert (body["hours"], body["check_id"]) == (4, str(check.id))
    assert 1 <= len(body["points"]) <= 2
    assert any(p["success"] is False for p in body["points"])


@pytest.mark.parametrize("hours", [0, 721])
async def test_hours_outside_a_month_are_refused(
    db: AsyncSession, client: AsyncClient, hours: int
) -> None:
    viewer = await _as(db, "viewer")
    check = await _check(db)

    resp = await client.get(
        f"{CHECKS}/{check.id}/history", params={"hours": hours}, headers=viewer
    )

    assert resp.status_code == 422


# ── Metrics: what a viewer reads of them ─────────────────────────────────────

HTTP_METRICS = {
    "response": {
        "status_code": 200,
        "headers": {"set-cookie": "sessionid=LIVE-SESSION-abc123; HttpOnly"},
        "ssl_certificate": {"days_until_expiry": 40, "issuer": "Example CA"},
    }
}


async def test_a_viewer_reads_no_response_headers_but_the_certificate(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _as(db, "viewer")
    editor = await _as(db, "editor")
    check = await _check(db)
    await _result(db, check, 5, metrics=json.dumps(HTTP_METRICS))

    seen = await client.get(f"{CHECKS}/{check.id}/results", headers=viewer)
    full = await client.get(f"{CHECKS}/{check.id}/results", headers=editor)

    assert "LIVE-SESSION-abc123" not in seen.text
    assert seen.json()["items"][0]["metrics"] == {
        "response": {"ssl_certificate": HTTP_METRICS["response"]["ssl_certificate"]}
    }
    assert full.json()["items"][0]["metrics"] == HTTP_METRICS


async def test_a_viewer_reads_a_synthetic_checks_errors_masked_in_metrics(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _as(db, "viewer")
    check = await _check(db)
    metrics = {"synthetic": {"status": "failure", "errors": [DSN_ERROR], "steps": []}}
    result = await _result(
        db, check, 5, success=False, error=DSN_ERROR, metrics=json.dumps(metrics)
    )

    page = await client.get(f"{CHECKS}/{check.id}/results", headers=viewer)
    one = await client.get(f"{CHECKS}/{check.id}/results/{result.id}", headers=viewer)
    summary = await client.get(f"{CHECKS}/{check.id}/summary", headers=viewer)

    for resp in (page, one, summary):
        assert "s3cret-pw" not in resp.text
    assert page.json()["items"][0]["metrics"]["synthetic"]["errors"] == [
        "connect to postgresql://***@db.internal:5432/app failed"
    ]


# ── Edges ────────────────────────────────────────────────────────────────────


async def test_a_month_of_history_reaches_the_newest_hour(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _as(db, "viewer")
    check = await _check(db)
    for hour in range(700):
        db.add(
            make_check_result(
                check_id=check.id,
                agent_id=check.agent_id,
                timestamp=utc_now() - timedelta(hours=hour, minutes=1),
            )
        )
    await db.flush()

    resp = await client.get(
        f"{CHECKS}/{check.id}/history", params={"hours": 720}, headers=viewer
    )

    points = resp.json()["points"]
    assert len(points) <= 500
    newest = max(p["bucket_time"] for p in points)
    oldest = min(p["bucket_time"] for p in points)
    assert newest > (utc_now() - timedelta(hours=3)).isoformat()
    assert oldest < (utc_now() - timedelta(hours=690)).isoformat()


async def test_a_result_id_two_checks_share_reads_through_each(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _as(db, "viewer")
    a, b = await _check(db), await _check(db)
    shared = uuid4()
    await _result(db, a, 5, id=shared, latency_ms=1.0)
    await _result(db, b, 10, id=shared, latency_ms=2.0)

    ra = await client.get(f"{CHECKS}/{a.id}/results/{shared}", headers=viewer)
    rb = await client.get(f"{CHECKS}/{b.id}/results/{shared}", headers=viewer)

    assert (ra.status_code, rb.status_code) == (200, 200)
    assert (ra.json()["check_id"], rb.json()["check_id"]) == (str(a.id), str(b.id))


@pytest.mark.parametrize("path", ["/{id}/results", ""])
async def test_a_page_past_any_result_set_is_refused_not_a_server_error(
    db: AsyncSession, client: AsyncClient, path: str
) -> None:
    viewer = await _as(db, "viewer")
    check = await _check(db)

    resp = await client.get(
        CHECKS + path.format(id=check.id), params={"page": 10**19}, headers=viewer
    )

    assert resp.status_code == 422, resp.text
