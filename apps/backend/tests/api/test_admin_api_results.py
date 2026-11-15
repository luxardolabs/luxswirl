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
        db, user.id, ApiTokenCreate(name=role, scopes=["*"])
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


async def test_an_empty_since_or_until_reads_as_absent(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _as(db, "viewer")
    check = await _check(db)
    await _result(db, check, 5)

    resp = await client.get(
        f"{CHECKS}/{check.id}/results",
        params={"since": "", "until": ""},
        headers=viewer,
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["total"] == 1


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


async def test_a_viewer_reads_no_text_of_a_synthetic_section(
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
    assert page.json()["items"][0]["metrics"]["synthetic"] == {
        "status": "failure",
        "browser_timing": {},
        "track_durations": {},
        "track_timings": {},
    }


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


# ── A DNS-over-HTTPS nameserver and the calendar's edges ─────────────────────

DOH_TOKEN = "f00dfacecafe1234"
DOH = f"https://doh.example.net/{DOH_TOKEN}"


async def test_a_viewer_reads_a_doh_nameserver_cut_to_its_origin(
    db: AsyncSession, client: AsyncClient
) -> None:
    viewer = await _as(db, "viewer")
    editor = await _as(db, "editor")
    check = await _check(db)
    metrics = {"dns": {"nameserver": DOH, "port": 443, "records": ["192.0.2.1"]}}
    result = await _result(db, check, 5, metrics=json.dumps(metrics))

    page = await client.get(f"{CHECKS}/{check.id}/results", headers=viewer)
    one = await client.get(f"{CHECKS}/{check.id}/results/{result.id}", headers=viewer)
    summary = await client.get(f"{CHECKS}/{check.id}/summary", headers=viewer)
    full = await client.get(f"{CHECKS}/{check.id}/results", headers=editor)

    for resp in (page, one, summary):
        assert DOH_TOKEN not in resp.text
    assert page.json()["items"][0]["metrics"]["dns"] == {
        "nameserver": "https://doh.example.net",
        "port": 443,
        "records": ["192.0.2.1"],
    }
    assert full.json()["items"][0]["metrics"]["dns"]["nameserver"] == DOH


# dnspython's refusal of a nameserver it cannot use, as an agent stores it.
DOH_HTTP = f"http://doh.example.net/{DOH_TOKEN}/dns-query"
DOH_ERROR = (
    f"Invalid nameserver: nameserver {DOH_HTTP} is not a "
    "dns.nameserver.Nameserver instance or text form, IP address, nor a valid "
    "https URL"
)


@pytest.mark.parametrize(
    ("check_config", "metrics"),
    [
        (None, {"dns": {"nameserver": DOH_HTTP, "port": 53}}),
        ({"nameserver": DOH_HTTP, "record_type": "A"}, None),
    ],
    ids=["named-by-the-result", "named-by-the-check"],
)
async def test_a_viewer_never_reads_a_doh_nameserver_in_a_results_error(
    db: AsyncSession, client: AsyncClient, check_config, metrics
) -> None:
    viewer = await _as(db, "viewer")
    editor = await _as(db, "editor")
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(agent_id=agent.id, check_type="dns", check_config=check_config)
    db.add(check)
    await db.flush()
    result = await _result(
        db,
        check,
        5,
        success=False,
        error=DOH_ERROR,
        metrics=json.dumps(metrics) if metrics else None,
    )

    reads = [
        await client.get(f"{CHECKS}/{check.id}/results", headers=viewer),
        await client.get(f"{CHECKS}/{check.id}/results/{result.id}", headers=viewer),
        await client.get(f"{CHECKS}/{check.id}/summary", headers=viewer),
        await client.get(f"{CHECKS}/{check.id}/history", headers=viewer),
    ]
    full = await client.get(f"{CHECKS}/{check.id}/results", headers=editor)

    for resp in reads:
        assert resp.status_code == 200, resp.text
        assert DOH_TOKEN not in resp.text, resp.text
    shown = reads[0].json()["items"][0]["error"]
    assert shown.startswith("Invalid nameserver: nameserver http://doh.example.net")
    assert full.json()["items"][0]["error"] == DOH_ERROR


@pytest.mark.parametrize(
    ("name", "moment"),
    [
        ("until", "0001-01-01T00:00:00"),
        ("until", "0001-01-01T00:30:00+01:00"),
        ("until", "9999-12-31T23:59:59-01:00"),
        ("since", "0001-01-01T00:00:00"),
        ("since", "1969-12-31T23:00:00Z"),
    ],
)
async def test_a_moment_outside_the_results_calendar_is_refused(
    db: AsyncSession, client: AsyncClient, name: str, moment: str
) -> None:
    viewer = await _as(db, "viewer")
    check = await _check(db)

    resp = await client.get(
        f"{CHECKS}/{check.id}/results", params={name: moment}, headers=viewer
    )

    assert resp.status_code == 422, resp.text


# ── A synthetic check's result: its exception type alone, for a viewer ───────

LINK_ID = "9f8e7d6c5b4a39281706"
# What a synthetic script's failure quotes of the page it opened, in the forms free
# text takes: JSON-escaped (`\/`), scheme-relative, a quote in the path, and
# Playwright's own error with its call log.
SYNTHETIC_LEAKS = {
    "json_escaped": (
        "Script execution failed: login refused: "
        '{"next":"https:\\/\\/app.example.com\\/reset\\/' + LINK_ID + '"}'
    ),
    "scheme_relative": (
        "Script execution failed: Refused to load the script "
        f"'//app.example.com/reset/{LINK_ID}' because it violates the CSP"
    ),
    "apostrophe_in_path": (
        "Script execution failed: page.goto: net::ERR_ABORTED at "
        f"https://app.example.com/o'brien/{LINK_ID}"
    ),
    "playwright_goto": (
        "Script execution failed: Page.goto: net::ERR_NAME_NOT_RESOLVED at "
        f"https://app.example.com/reset/{LINK_ID}\nCall log:\n"
        f'  - navigating to "https://app.example.com/reset/{LINK_ID}", '
        'waiting until "load"'
    ),
}
SHOWN_AS = {
    "json_escaped": "Synthetic check failed",
    "scheme_relative": "Synthetic check failed",
    "apostrophe_in_path": "Synthetic check failed",
    "playwright_goto": "Synthetic check failed: Page.goto",
}


def _synthetic_metrics(leak: str) -> dict[str, object]:
    return {
        "synthetic": {
            "status": "failure",
            "errors": [leak],
            "console_errors": [leak],
            "request_failures": [{"url": leak, "failure": leak}],
            "steps": [leak],
            "track_durations": {leak: 1.5},
            "track_timings": {leak: {"navigationStart": 1000, "loadEventEnd": 1222}},
            "browser_timing": {"navigationStart": 1000, "loadEventEnd": 1300},
            "script_execution_time_ms": 1500.0,
            "total_execution_time_ms": 2400.0,
            "next_page": leak,
        }
    }


@pytest.mark.parametrize("leak", list(SYNTHETIC_LEAKS))
async def test_a_viewer_reads_a_synthetic_result_as_its_exception_type_alone(
    db: AsyncSession, client: AsyncClient, leak: str
) -> None:
    error = SYNTHETIC_LEAKS[leak]
    viewer = await _as(db, "viewer")
    editor = await _as(db, "editor")
    agent = make_agent()
    db.add(agent)
    await db.flush()
    check = make_check(
        agent_id=agent.id, check_type="synthetic", target="https://app.example.com"
    )
    db.add(check)
    await db.flush()
    metrics = _synthetic_metrics(error)
    result = await _result(
        db, check, 5, success=False, error=error, metrics=json.dumps(metrics)
    )

    reads = [
        await client.get(f"{CHECKS}/{check.id}/results", headers=viewer),
        await client.get(f"{CHECKS}/{check.id}/results/{result.id}", headers=viewer),
        await client.get(f"{CHECKS}/{check.id}/summary", headers=viewer),
        await client.get(f"{CHECKS}/{check.id}/history", headers=viewer),
    ]
    full = await client.get(f"{CHECKS}/{check.id}/results", headers=editor)

    for resp in reads:
        assert resp.status_code == 200, resp.text
        assert LINK_ID not in resp.text, resp.text
        assert "reset" not in resp.text, resp.text
    shown = reads[0].json()["items"][0]
    assert shown["error"] == SHOWN_AS[leak]
    assert [p["error"] for p in reads[3].json()["points"] if p["error"]] == [
        SHOWN_AS[leak]
    ]
    assert shown["metrics"] == {
        "synthetic": {
            "status": "failure",
            "script_execution_time_ms": 1500.0,
            "total_execution_time_ms": 2400.0,
            "browser_timing": {"navigationStart": 1000, "loadEventEnd": 1300},
            "track_durations": {"Step 1": 1.5},
            "track_timings": {
                "Step 1": {"navigationStart": 1000, "loadEventEnd": 1222}
            },
        }
    }
    assert full.json()["items"][0]["error"] == error
    assert full.json()["items"][0]["metrics"] == metrics


# Text that is no URL, in a check that runs no script: a viewer reads it as written.
PLAIN_ERRORS = [
    "expected agent 1.2.3/4 got 1.2.2",
    "Error: ENOENT C:/Users/ops/report.txt",
    "at Object.<anonymous> (node:internal/modules/cjs/loader:1105:14)",
]


@pytest.mark.parametrize("error", PLAIN_ERRORS)
async def test_a_viewer_reads_a_non_synthetic_error_that_names_no_url_as_written(
    db: AsyncSession, client: AsyncClient, error: str
) -> None:
    viewer = await _as(db, "viewer")
    check = await _check(db)
    result = await _result(db, check, 5, success=False, error=error)

    page = await client.get(f"{CHECKS}/{check.id}/results", headers=viewer)
    one = await client.get(f"{CHECKS}/{check.id}/results/{result.id}", headers=viewer)
    history = await client.get(f"{CHECKS}/{check.id}/history", headers=viewer)

    assert page.json()["items"][0]["error"] == error
    assert one.json()["error"] == error
    assert [p["error"] for p in history.json()["points"] if p["error"]] == [error]
