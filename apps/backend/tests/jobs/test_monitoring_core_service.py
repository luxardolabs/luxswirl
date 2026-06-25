"""Correctness tests for monitoring_core_service (LUXSWIRL-127).

Unlike the cleanups, these collectors are READ-ONLY — they COUNT live state
(agents, checks, alerts, statuses) and push gauges into the in-memory Prometheus
collector; they don't write. So the assertion here is correctness of the
aggregation, not persistence: seed known rows, run the collector against the test
DB, and assert the counts match — including the active-vs-stale cutoff logic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fixtures.factories import make_agent, make_alert, make_check

from app.services.core.monitoring_core_service import (
    collect_database_metrics,
    collect_operational_metrics,
)

pytestmark = pytest.mark.integration


class TestCollectDatabaseMetrics:
    async def test_counts_agents_checks_and_active_within_cutoff(self, worker_db):
        now = datetime.now(UTC)
        async with worker_db() as s:
            active = make_agent(agent_name="active", last_seen=now)
            stale = make_agent(agent_name="stale", last_seen=now - timedelta(hours=1))
            s.add(active)
            s.add(stale)
            await s.flush()
            s.add(make_check(agent_id=active.id))
            s.add(make_check(agent_id=active.id))
            await s.commit()

        metrics = await collect_database_metrics()

        assert metrics["agent_count"] == 2
        assert metrics["check_count"] == 2
        # active = last_seen within the 300s window; only `active` qualifies.
        assert metrics["active_agent_count"] == 1


class TestCollectOperationalMetrics:
    async def test_counts_only_enabled_alerts(self, worker_db):
        async with worker_db() as s:
            s.add(make_alert(is_enabled=True))
            s.add(make_alert(is_enabled=True))
            s.add(make_alert(is_enabled=False))
            await s.commit()

        operational = await collect_operational_metrics()

        assert operational["alerts_enabled"] == 2
