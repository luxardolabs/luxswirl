"""The dashboard row must carry everything the templates read off it.

The dashboard used to render ORM Check objects with `latest_status`,
`latest_success` and `latency_ms` setattr'd onto them. Those three fields exist
on no Check column, so nothing in the type system connected the template's
`check.latest_status` to the line that attached it — rename either side and the
page breaks at render time, on a surface the suite renders with no data.

DashboardCheck replaces that. These tests read the projection the way the
templates do, so a field that stops being carried fails here instead of on the
dashboard.
"""

from __future__ import annotations

import dataclasses
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.datetime_utils import utc_now
from app.services.views.status_view_service import StatusViewService
from fixtures.factories import make_agent, make_check, make_check_result
from sqlalchemy.ext.asyncio import AsyncSession


async def _agent_with_check(db: AsyncSession, **check_kw):
    agent = make_agent(agent_name="edge-01", tags=["prod", "eu"])
    db.add(agent)
    await db.flush()
    check = make_check(
        agent_id=agent.id,
        display_name="api-health",
        check_type="http",
        target="https://example.invalid/health",
        tags=["public"],
        **check_kw,
    )
    db.add(check)
    await db.flush()
    return agent, check


class TestEveryTemplateFieldIsCarried:
    async def test_the_row_matches_the_check_and_its_agent(self, db: AsyncSession):
        agent, check = await _agent_with_check(db)
        db.add(
            make_check_result(
                check_id=check.id,
                agent_id=agent.id,
                timestamp=utc_now() - timedelta(minutes=1),
                success=True,
                latency_ms=42.5,
            )
        )
        await db.flush()

        status_rows, _ = await StatusViewService.get_all_checks_status(db)
        rows = [r.to_dashboard_check() for r in status_rows]

        row = next(r for r in rows if r.id == check.id)
        # Exactly the names dashboard_items.html dereferences.
        assert row.display_name == "api-health"
        assert row.check_type == "http"
        assert row.target == "https://example.invalid/health"
        assert row.tags == ["public"]
        assert row.enabled is True
        assert row.agent.id == agent.id
        assert row.agent.agent_name == "edge-01"
        assert row.agent.tags == ["prod", "eu"]
        assert row.latency_ms == 42.5
        assert row.latest_success is True
        assert row.latest_status

    async def test_the_agent_is_a_value_not_a_relationship(self, db: AsyncSession):
        """`check.agent.tags` on a detached ORM row lazy-loads and raises
        MissingGreenlet. The row carries flat values so it cannot."""
        agent, check = await _agent_with_check(db)
        await db.flush()

        status_rows, _ = await StatusViewService.get_all_checks_status(db)
        rows = [r.to_dashboard_check() for r in status_rows]
        row = next(r for r in rows if r.id == check.id)

        # Readable with no session in scope at all.
        assert (row.agent.agent_name, row.agent.tags) == ("edge-01", ["prod", "eu"])

    async def test_a_check_with_no_results_still_appears_with_a_status(
        self, db: AsyncSession
    ):
        """The old map returned such a check WITHOUT the status attributes, so a
        template reading latest_status hit a missing attribute. A dataclass
        cannot be half-populated."""
        agent, check = await _agent_with_check(db)
        await db.flush()

        status_rows, _ = await StatusViewService.get_all_checks_status(db)
        rows = [r.to_dashboard_check() for r in status_rows]

        row = next(r for r in rows if r.id == check.id)
        assert row.latest_status is not None
        assert row.latest_success is None  # never reported
        assert row.latency_ms is None


class TestStatsAreReplacedNotMutated:
    async def test_the_row_is_frozen(self, db: AsyncSession):
        """The stats pass uses dataclasses.replace. If the row were mutable the
        old setattr pattern would silently work again."""
        agent, check = await _agent_with_check(db)
        await db.flush()
        status_rows, _ = await StatusViewService.get_all_checks_status(db)
        rows = [r.to_dashboard_check() for r in status_rows]
        row = rows[0]

        assert dataclasses.is_dataclass(row)
        try:
            row.latest_status = "tampered"
        except dataclasses.FrozenInstanceError:
            return
        raise AssertionError("DashboardCheck must be frozen")
