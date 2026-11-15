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
from app.core.datetime_utils import utc_now
from app.core.rate_limit import limiter
from app.main import app
from app.services.core.agent_core_service import AgentCoreService
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
    ("check_type", "metrics", "still_shown"),
    [
        ("http", "{}", "could not connect to"),
        ("synthetic", METRICS, "Synthetic check failed"),
        # A run whose metrics carry a synthetic section is a script's, whatever
        # the check's type is now.
        ("http", METRICS, "Synthetic check failed"),
    ],
)
async def test_a_viewer_reads_the_panel_masked(
    client: AsyncClient,
    db: AsyncSession,
    check_type: str,
    metrics: str,
    still_shown: str,
) -> None:
    panel = await _panel(client, db, check_type, "viewer", metrics)

    assert still_shown in panel
    assert SECRET not in panel


DOH_TOKEN = "f00dfacecafe1234"
DOH_METRICS = json.dumps(
    {
        "dns": {
            "nameserver": f"https://doh.example.net/{DOH_TOKEN}",
            "port": 443,
            "record_type": "A",
            "record_count": 1,
            "records": ["192.0.2.1"],
            "ttl": 300,
            "authoritative": False,
            "recursion_available": True,
            "recursion_desired": True,
            "truncated": False,
            "canonical_name": "example.com.",
        }
    }
)


async def test_a_viewer_reads_a_doh_nameserver_as_its_origin(
    client: AsyncClient, db: AsyncSession
) -> None:
    viewer = await _panel(client, db, "dns", "viewer", DOH_METRICS)
    editor = await _panel(client, db, "dns", "editor", DOH_METRICS)

    assert DOH_TOKEN not in viewer
    assert "https://doh.example.net:443" in viewer
    assert DOH_TOKEN in editor


async def test_a_viewer_never_reads_a_doh_nameserver_in_a_results_error(
    client: AsyncClient, db: AsyncSession
) -> None:
    nameserver = f"http://doh.example.net/{DOH_TOKEN}/dns-query"
    error = f"Invalid nameserver: nameserver {nameserver} is not a valid https URL"
    agent = make_agent()
    user = make_user(role="viewer")
    db.add_all([agent, user])
    await db.flush()
    check = make_check(
        agent_id=agent.id,
        check_type="dns",
        check_config={"nameserver": nameserver, "record_type": "A"},
    )
    db.add(check)
    await db.flush()
    db.add(
        make_check_result(
            check_id=check.id, agent_id=agent.id, success=False, error=error
        )
    )
    await db.flush()
    _, session = await AuthCoreService().create_session(db, user.id)
    client.cookies.set(settings.security.session_cookie_name, session)

    resp = await client.get(f"/check/{check.id}")

    assert resp.status_code == 200, resp.text
    assert "Invalid nameserver" in resp.text
    assert DOH_TOKEN not in resp.text


async def test_an_editor_reads_the_error_as_it_was_reported(
    client: AsyncClient, db: AsyncSession
) -> None:
    panel = await _panel(client, db, "http", "editor")

    assert SECRET in panel


async def test_a_step_named_for_a_url_reads_as_its_number(
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

    assert "Step 1" in panel
    assert "app.example.com/login" not in panel
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


LINK_ID = "9f8e7d6c5b4a39281706"
# What a synthetic script's failure quotes of the page it opened: JSON-escaped,
# scheme-relative, a quote in the path, Playwright's own error.
SYNTHETIC_LEAKS = [
    'Script execution failed: refused: {"next":"https:\\/\\/app.example.com\\/reset\\/'
    + LINK_ID
    + '"}',
    f"Script execution failed: Refused to load '//app.example.com/reset/{LINK_ID}'",
    f"Script execution failed: page.goto: at https://app.example.com/o'brien/{LINK_ID}",
    "Script execution failed: Page.goto: net::ERR_NAME_NOT_RESOLVED at "
    f"https://app.example.com/reset/{LINK_ID}",
]


@pytest.mark.parametrize("error", SYNTHETIC_LEAKS)
@pytest.mark.parametrize(("role", "shown"), [("viewer", False), ("editor", True)])
async def test_a_viewer_reads_a_synthetic_result_as_its_exception_type_alone(
    client: AsyncClient, db: AsyncSession, error: str, role: str, shown: bool
) -> None:
    """The latest result's synthetic section, the history and Recent Events."""
    agent = make_agent()
    user = make_user(role=role)
    db.add_all([agent, user])
    await db.flush()
    check = make_check(
        agent_id=agent.id, check_type="synthetic", target="https://app.example.com"
    )
    db.add(check)
    await db.flush()
    metrics = {
        "synthetic": {
            "status": "failure",
            "errors": [error],
            "console_errors": [error],
            "request_failures": [{"url": error, "failure": error}],
            "steps": [error],
            "track_durations": {error: 1.5},
            "track_timings": {error: {"navigationStart": 1000, "loadEventEnd": 1222}},
        }
    }
    db.add(
        make_check_result(
            check_id=check.id,
            agent_id=agent.id,
            success=False,
            error=error,
            metrics=json.dumps(metrics),
        )
    )
    await db.flush()
    _, session = await AuthCoreService().create_session(db, user.id)
    client.cookies.set(settings.security.session_cookie_name, session)

    resp = await client.get(f"/check/{check.id}")

    assert resp.status_code == 200, resp.text
    assert (LINK_ID in resp.text) is shown
    assert ("Synthetic check failed" in resp.text) is not shown
    if not shown:
        assert "Step 1" in resp.text
        assert "1500" in resp.text


async def _panel_of_a_retyped_check(
    client: AsyncClient, db: AsyncSession, role: str, metrics: str | None
) -> str:
    """A synthetic check that failed, then retyped to http by an admin."""
    agent = make_agent()
    user = make_user(role=role)
    db.add_all([agent, user])
    await db.flush()
    check = make_check(
        agent_id=agent.id, check_type="synthetic", target="https://app.example.com"
    )
    db.add(check)
    await db.flush()
    error = (
        "Script execution failed: Page.goto: net::ERR_CONNECTION_REFUSED at "
        f"https://app.example.com/password-reset/{LINK_ID}"
    )
    db.add(
        make_check_result(
            check_id=check.id,
            agent_id=agent.id,
            success=False,
            error=error,
            metrics=metrics,
        )
    )
    check.check_type = "http"
    await db.flush()
    _, session = await AuthCoreService().create_session(db, user.id)
    client.cookies.set(settings.security.session_cookie_name, session)

    resp = await client.get(f"/check/{check.id}")

    assert resp.status_code == 200, resp.text
    return resp.text


@pytest.mark.parametrize(
    "metrics",
    [
        json.dumps({"synthetic": {"status": "failure", "errors": [LINK_ID]}}),
        # The agent cut the run's oversized metrics to a size note.
        json.dumps({"note": "metrics too large"}),
        None,
    ],
)
@pytest.mark.parametrize(("role", "shown"), [("viewer", False), ("editor", True)])
async def test_a_check_retyped_from_synthetic_shows_a_viewer_no_old_script_text(
    client: AsyncClient,
    db: AsyncSession,
    metrics: str | None,
    role: str,
    shown: bool,
) -> None:
    """The history and Recent Events: the old runs are a script's output."""
    panel = await _panel_of_a_retyped_check(client, db, role, metrics)

    assert (LINK_ID in panel) is shown
    if not shown:
        assert "Synthetic check failed: Page.goto" in panel


# A synthetic section's figures the agent sent as anything but a finite number.
ODD_SYNTHETIC: dict[str, object] = {
    "null_mark": {
        "status": "success",
        "browser_timing": {
            "navigationStart": None,
            "domLoading": 1200,
            "domComplete": 1500,
            "loadEventEnd": 1700,
        },
    },
    "text_figures": {
        "status": 7,
        "track_durations": {"a": "1.5"},
        "track_timings": {"a": "x", "b": [1]},
        "browser_timing": {"navigationStart": "1", "domLoading": 5},
        "total_execution_time_ms": "12",
    },
    "nulls": {
        "status": None,
        "track_durations": None,
        "track_timings": None,
        "browser_timing": None,
        "total_execution_time_ms": None,
    },
    "huge": {"status": "failure", "track_durations": {"a": 1e308}},
    "big_int": {
        "status": "failure",
        "track_durations": {"a": 10**40},
        "total_execution_time_ms": 10**40,
        "browser_timing": {"navigationStart": 1, "domLoading": 10**40},
    },
    "not_a_mapping": ["status", "failure"],
}


@pytest.mark.parametrize("role", ["viewer", "editor"])
@pytest.mark.parametrize("case", list(ODD_SYNTHETIC))
async def test_the_panel_renders_a_synthetic_section_with_odd_figures(
    client: AsyncClient, db: AsyncSession, case: str, role: str
) -> None:
    metrics = json.dumps({"synthetic": ODD_SYNTHETIC[case]})

    panel = await _panel(client, db, "synthetic", role, metrics)

    assert "Synthetic Monitor" in panel


@pytest.mark.parametrize("role", ["viewer", "editor"])
async def test_a_timing_mark_that_is_no_figure_reads_as_a_dash(
    client: AsyncClient, db: AsyncSession, role: str
) -> None:
    metrics = json.dumps({"synthetic": ODD_SYNTHETIC["null_mark"]})

    panel = await _panel(client, db, "synthetic", role, metrics)

    assert "Page Load Performance" in panel
    assert "DOM Load (ms)" in panel
    assert "—" in panel


RESET_TOKEN = "Pr1vateResetT0ken"
# The agent's size note: a run whose metrics outgrew the server's limit.
SIZE_NOTE = json.dumps({"metrics_truncated": True, "metrics_original_chars": 90000})


@pytest.mark.parametrize(
    "error",
    [
        "Script execution failed: TimeoutError: Page.goto: Timeout 30000ms exceeded "
        f"navigating to https://app.example.com/reset/{RESET_TOKEN}",
        f"Script compilation failed: SyntaxError: bad line 'https://x/{RESET_TOKEN}'",
        f"Playwright initialization failed: Error: https://x/{RESET_TOKEN}",
        f"Script execution timed out after 30s https://x/{RESET_TOKEN}",
    ],
    ids=["execution", "compilation", "initialization", "timed-out"],
)
async def test_a_viewer_reads_a_size_noted_synthetic_error_as_a_synthetic_one(
    client: AsyncClient, db: AsyncSession, error: str
) -> None:
    """A check never marked as synthetic (retyped before the column existed),
    whose run reached the server as the size note: the agent's own failure
    prefix still says a script ran."""
    agent = make_agent()
    viewer = make_user(role="viewer")
    db.add_all([agent, viewer])
    await db.flush()
    check = make_check(agent_id=agent.id, check_type="http")
    db.add(check)
    await db.flush()
    assert check.ran_synthetic is False
    db.add(
        make_check_result(
            check_id=check.id,
            agent_id=agent.id,
            success=False,
            error=error,
            metrics=SIZE_NOTE,
        )
    )
    await db.flush()
    _, session = await AuthCoreService().create_session(db, viewer.id)
    client.cookies.set(settings.security.session_cookie_name, session)

    resp = await client.get(f"/check/{check.id}")

    assert resp.status_code == 200, resp.text
    assert RESET_TOKEN not in resp.text
    assert "Synthetic check failed" in resp.text


HUGE = 10**400  # a JSON integer literal: json.loads reads it as a Python int


def _timing_with(dom_loading: object) -> str:
    return json.dumps(
        {
            "synthetic": {
                "status": "failure",
                "browser_timing": {
                    "navigationStart": 1.5,
                    "domLoading": dom_loading,
                    "domComplete": 3.0,
                    "loadEventEnd": 4.0,
                },
            }
        }
    )


@pytest.mark.parametrize("role", ["viewer", "editor"])
async def test_huge_integer_timing_mark_does_not_500_the_panel(
    client: AsyncClient, db: AsyncSession, role: str
) -> None:
    panel = await _panel(client, db, "synthetic", role, _timing_with(HUGE))

    assert "Page Load Performance" in panel


@pytest.mark.parametrize("role", ["viewer", "editor"])
async def test_huge_integer_step_mark_does_not_500_the_panel(
    client: AsyncClient, db: AsyncSession, role: str
) -> None:
    metrics = json.dumps(
        {
            "synthetic": {
                "status": "failure",
                "track_durations": {"home": 1.0},
                "track_timings": {"home": {"navigationStart": 0.5, "domLoading": HUGE}},
            }
        }
    )

    panel = await _panel(client, db, "synthetic", role, metrics)

    assert "Multi-Step Navigation Performance" in panel


async def test_agent_reported_huge_timing_500s_the_viewer_panel(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: an agent reports a timing mark as a JSON integer too big for a
    float; ingestion stores it, and a viewer's panel still renders."""
    monkeypatch.setattr(settings.security, "auth_enabled", True)
    limiter.reset()
    agent = make_agent(approval_status="pending")
    viewer = make_user(role="viewer")
    db.add_all([agent, viewer])
    await db.flush()
    _, key = await AgentCoreService.approve_agent(db, agent.id, actor_is_admin=True)
    check = make_check(agent_id=agent.id, check_type="synthetic")
    db.add(check)
    await db.flush()
    resp = await client.post(
        "/api/v1/agent/reports",
        json={
            "agent_id": str(agent.id),
            "checks": [
                {
                    "check_id": str(check.id),
                    "display_name": "probe",
                    "check_type": "synthetic",
                    "target": "https://example.com",
                    "timestamp": utc_now().isoformat(),
                    "success": False,
                    "latency_ms": 5.0,
                    "error": "Script execution failed: ValueError: x",
                    "metrics": json.loads(_timing_with(HUGE)),
                }
            ],
        },
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["results_processed"] == 1, resp.text
    _, session = await AuthCoreService().create_session(db, viewer.id)
    client.cookies.set(settings.security.session_cookie_name, session)

    panel = await client.get(f"/check/{check.id}")

    assert panel.status_code == 200, panel.text[:200]


@pytest.mark.parametrize(
    "section",
    [
        {"status": "failure", "steps": 5},
        {"status": "failure", "console_errors": 5},
        {"status": "failure", "request_failures": 5},
        {"status": "failure", "track_durations": 5},
        {"status": "failure", "track_timings": "x"},
        {"status": "failure", "browser_timing": [1, 2]},
        {"status": "failure", "track_durations": ["a"], "track_timings": [1]},
    ],
    ids=[
        "steps-int",
        "console-errors-int",
        "request-failures-int",
        "track-durations-int",
        "track-timings-text",
        "browser-timing-list",
        "track-lists",
    ],
)
@pytest.mark.parametrize("role", ["viewer", "editor"])
async def test_non_list_script_fields_do_not_500_the_panel(
    client: AsyncClient, db: AsyncSession, role: str, section: dict[str, object]
) -> None:
    panel = await _panel(
        client, db, "synthetic", role, json.dumps({"synthetic": section})
    )

    assert "Synthetic Monitor" in panel
