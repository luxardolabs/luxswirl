"""The check detail panel shows a viewer a result's error and metrics masked.

The admin API does the same for the same viewer (`AdminCheckResultCoreService`). A
monitored system's error text can quote a connection string, and a synthetic check's
console errors can quote what the page logged.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.credential_mask import mask_mapping
from app.main import app
from app.services.core.auth_core_service import AuthCoreService
from fixtures.factories import make_agent, make_check, make_check_result, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

SECRET = "S3cretZZ9"
ERROR = f"could not connect to postgresql://monitor:{SECRET}@db.internal:5432/app"
METRICS = json.dumps(
    {
        "synthetic": {
            "status": "failed",
            "console_errors": [
                f"fetch failed https://monitor:{SECRET}@app.example.com/"
            ],
        }
    }
)


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"https://{settings.allowed_hosts[0]}",
    ) as c:
        yield c


async def _panel(
    client: AsyncClient,
    db: AsyncSession,
    check_type: str,
    role: str,
    metrics: str = METRICS,
) -> str:
    agent = make_agent()
    user = make_user(role=role)
    db.add_all([agent, user])
    await db.flush()
    check = make_check(agent_id=agent.id, check_type=check_type)
    db.add(check)
    await db.flush()
    db.add(
        make_check_result(
            check_id=check.id,
            agent_id=agent.id,
            success=False,
            error=ERROR,
            metrics=metrics,
        )
    )
    await db.flush()
    _, session = await AuthCoreService().create_session(db, user.id)
    client.cookies.set(settings.security.session_cookie_name, session)

    resp = await client.get(f"/check/{check.id}")

    assert resp.status_code == 200, resp.text
    return resp.text


@pytest.mark.parametrize(
    ("check_type", "still_shown"),
    [("http", "could not connect to"), ("synthetic", "app.example.com")],
)
async def test_a_viewer_reads_the_panel_masked(
    client: AsyncClient, db: AsyncSession, check_type: str, still_shown: str
) -> None:
    panel = await _panel(client, db, check_type, "viewer")

    assert still_shown in panel
    assert SECRET not in panel


async def test_an_editor_reads_the_error_as_it_was_reported(
    client: AsyncClient, db: AsyncSession
) -> None:
    panel = await _panel(client, db, "http", "editor")

    assert SECRET in panel


async def test_a_step_named_for_a_url_reads_masked(
    client: AsyncClient, db: AsyncSession
) -> None:
    url = f"https://monitor:{SECRET}@app.example.com/login"
    metrics = json.dumps(
        {
            "synthetic": {
                "status": "passed",
                "track_timings": {url: {}},
                "track_durations": {url: 12.5},
            }
        }
    )

    panel = await _panel(client, db, "synthetic", "viewer", metrics)

    assert "app.example.com/login" in panel
    assert SECRET not in panel


def _nested(depth: int) -> dict[str, object]:
    value: dict[str, object] = {"leaf": f"postgresql://u:{SECRET}@db/app"}
    for _ in range(depth):
        value = {"a": value}
    return value


async def test_metrics_nested_very_deep_do_not_break_a_viewers_panel(
    client: AsyncClient, db: AsyncSession
) -> None:
    metrics = json.dumps({"dns": _nested(800)})

    panel = await _panel(client, db, "http", "viewer", metrics)

    assert SECRET not in panel


def test_a_value_past_the_depth_limit_is_masked_whole() -> None:
    masked = mask_mapping(_nested(800))

    assert SECRET not in json.dumps(masked)


def test_a_value_within_the_depth_limit_keeps_its_shape() -> None:
    masked: object = mask_mapping(_nested(50))

    for _ in range(50):
        assert isinstance(masked, dict)
        masked = masked["a"]
    assert masked == {"leaf": "postgresql://***@db/app"}


def _steps_differing_only_in_a_credential() -> tuple[str, str]:
    base = "https://app.example.com/login?token="
    return f"{base}AAA111", f"{base}BBB222"


@pytest.mark.parametrize("reversed_durations", [False, True])
async def test_two_steps_that_mask_alike_keep_their_own_rows(
    client: AsyncClient, db: AsyncSession, reversed_durations: bool
) -> None:
    first, second = _steps_differing_only_in_a_credential()
    durations = {first: 1.0, second: 2.0}
    if reversed_durations:
        durations = {second: 2.0, first: 1.0}
    metrics = json.dumps(
        {
            "synthetic": {
                "status": "passed",
                "track_timings": {
                    first: {"navigationStart": 1000, "loadEventEnd": 1111},
                    second: {"navigationStart": 1000, "loadEventEnd": 1222},
                },
                "track_durations": durations,
            }
        }
    )

    viewer = await _panel(client, db, "synthetic", "viewer", metrics)
    editor = await _panel(client, db, "synthetic", "editor", metrics)

    assert "AAA111" not in viewer
    assert "BBB222" not in viewer
    for number in ("111ms", "222ms", "1000", "2000", "3000"):
        assert (number in viewer) == (number in editor), number


def test_a_key_keeps_one_name_through_a_document() -> None:
    first, second = _steps_differing_only_in_a_credential()

    masked = mask_mapping(
        {"durations": {first: 1.0, second: 2.0}, "timings": {second: 20.0, first: 10.0}}
    )

    assert masked == {
        "durations": {
            "https://app.example.com/login?token=***": 1.0,
            "https://app.example.com/login?token=*** (2)": 2.0,
        },
        "timings": {
            "https://app.example.com/login?token=*** (2)": 20.0,
            "https://app.example.com/login?token=***": 10.0,
        },
    }


def test_a_key_missing_from_one_mapping_keeps_its_name_in_the_other() -> None:
    first, second = _steps_differing_only_in_a_credential()

    masked = mask_mapping(
        {"durations": {first: 1.0, second: 2.0}, "timings": {second: 20.0}}
    )

    durations = masked["durations"]
    timings = masked["timings"]
    assert isinstance(durations, dict)
    assert isinstance(timings, dict)
    [name] = timings
    assert durations[name] == 2.0


def test_a_key_that_already_reads_like_a_number_does_not_merge() -> None:
    first, second = _steps_differing_only_in_a_credential()
    literal = "https://app.example.com/login?token=*** (2)"

    for keys in ([first, second, literal], [literal, first, second]):
        masked = mask_mapping({key: index for index, key in enumerate(keys)})

        assert sorted(v for v in masked.values() if isinstance(v, int)) == [0, 1, 2]


def test_the_names_do_not_depend_on_how_the_secrets_sort() -> None:
    base = "https://app.example.com/login?token="

    low_first = mask_mapping({f"{base}AAA": 1.0, f"{base}ZZZ": 2.0})
    high_first = mask_mapping({f"{base}ZZZ": 1.0, f"{base}AAA": 2.0})

    assert low_first == high_first


async def test_a_step_with_no_timing_reads_the_same_figures_as_an_editor(
    client: AsyncClient, db: AsyncSession
) -> None:
    first, second = _steps_differing_only_in_a_credential()
    metrics = json.dumps(
        {
            "synthetic": {
                "status": "passed",
                "track_durations": {first: 1.0, second: 2.0},
                "track_timings": {
                    second: {"navigationStart": 1000, "loadEventEnd": 1222},
                },
            }
        }
    )

    viewer = await _panel(client, db, "synthetic", "viewer", metrics)
    editor = await _panel(client, db, "synthetic", "editor", metrics)

    assert "BBB222" not in viewer
    for number in ("1000ms", "2000ms", "3000"):
        assert (number in viewer) == (number in editor), number
