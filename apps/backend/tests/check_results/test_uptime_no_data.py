"""A check with no results in the window has no uptime, not 0 % or 100 %.

The 24 h uptime (and the detail panel's uptime over its range, the public
status page's per-check uptime, and the status summary's 24 h success rate) is
a share of the results in the window. With no results there is no share: the
value is None and every surface renders it as no data, while a check that
failed every time still reads 0 %. The admin API already returns
`success_rate_24h: null` for the same check.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.main import app
from app.models.agent_model import Agent
from app.models.check_model import Check
from app.models.status_page_model import StatusPage
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.check_result_core_service import CheckResultCoreService
from app.services.views.status_view_service import StatusViewService
from app.web.templates import templates
from fixtures.factories import make_agent, make_check, make_check_result, make_user
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def agent(db: AsyncSession) -> Agent:
    agent = make_agent()
    db.add(agent)
    await db.flush()
    return agent


@pytest_asyncio.fixture
async def client(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    admin = make_user(role="admin")
    db.add(admin)
    await db.flush()
    _, token = await AuthCoreService().create_session(db, admin.id)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as c:
        c.cookies.set(settings.security.session_cookie_name, token)
        yield c


async def _check(
    db: AsyncSession, agent: Agent, *, ago: timedelta | None, success: bool = True
) -> Check:
    """A check whose one result (if any) is `ago` old."""
    check = make_check(agent_id=agent.id)
    db.add(check)
    await db.flush()
    if ago is not None:
        db.add(
            make_check_result(
                check_id=check.id,
                agent_id=agent.id,
                success=success,
                timestamp=utc_now() - ago,
            )
        )
        await db.flush()
    return check


@pytest.mark.parametrize("ago", [None, timedelta(days=2)])
async def test_a_row_with_no_results_in_24h_has_no_uptime(
    db: AsyncSession, agent: Agent, ago: timedelta | None
) -> None:
    check = await _check(db, agent, ago=ago)

    [row], _ = await StatusViewService.get_all_checks_status(db, check_ids=[check.id])

    assert row.uptime_24h is None
    assert row.uptime_display == "-"
    assert row.to_dashboard_check().uptime_24h is None


async def test_a_row_that_failed_every_time_reads_zero(
    db: AsyncSession, agent: Agent
) -> None:
    check = await _check(db, agent, ago=timedelta(minutes=1), success=False)

    [row], _ = await StatusViewService.get_all_checks_status(db, check_ids=[check.id])

    assert row.uptime_24h == 0.0
    assert row.uptime_display == "0.0%"


async def test_the_public_status_page_shows_no_data(
    client: AsyncClient, db: AsyncSession, agent: Agent
) -> None:
    silent = await _check(db, agent, ago=None)
    failing = await _check(db, agent, ago=timedelta(minutes=1), success=False)
    grouped_silent = await _check(db, agent, ago=None)
    page = StatusPage(
        name="Quiet",
        slug=f"quiet-{uuid4().hex[:8]}",
        is_public=True,
        items=[
            {"type": "check", "order": 0, "check_id": str(silent.id)},
            {
                "type": "group",
                "order": 1,
                "name": "Core",
                # Sorted by uptime, lowest first: no data sorts below 0 %.
                "sort_by": "uptime",
                "sort_direction": "asc",
                "checks": [str(failing.id), str(grouped_silent.id)],
            },
        ],
    )
    db.add(page)
    await db.flush()

    resp = await client.get(f"/status/{page.slug}")

    assert resp.status_code == 200, resp.text[:500]
    shown = re.findall(r'title="24-hour uptime">\s*(.*?)\s*<', resp.text)
    assert shown == ["No data", "No data", "0.0%"]


async def test_the_detail_panel_shows_no_data(
    client: AsyncClient, db: AsyncSession, agent: Agent
) -> None:
    check = await _check(db, agent, ago=None)

    resp = await client.get(f"/check/{check.id}")

    assert resp.status_code == 200, resp.text[:500]
    [shown] = re.findall(r">Uptime</p>\s*<p[^>]*>\s*(.*?)\s*<", resp.text)
    assert shown == "-"


async def test_the_summary_success_rate_with_no_results_is_none(
    monkeypatch: pytest.MonkeyPatch, db: AsyncSession
) -> None:
    async def no_results(_db: AsyncSession) -> dict[str, int]:
        return {
            "total_checks": 1,
            "enabled_checks": 1,
            "up_count": 0,
            "down_count": 0,
            "active_agents": 1,
            "overall_total": 0,
            "overall_successful": 0,
        }

    monkeypatch.setattr(
        CheckResultCoreService, "get_status_summary_data", staticmethod(no_results)
    )

    summary = await StatusViewService.get_status_summary(db)

    assert summary["overall_success_rate"] is None
    html = templates.get_template("partials/status_summary.html").render(
        summary=summary
    )
    [shown] = re.findall(
        r"24h Success Rate</p>\s*<p[^>]*>\s*(.*?)\s*</p>", html, flags=re.S
    )
    assert shown == "-"
