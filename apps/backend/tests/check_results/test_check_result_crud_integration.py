"""Integration tests for CheckResultCRUD against a real TimescaleDB.

CheckResult is on a TimescaleDB hypertable (chunked by timestamp), so these
tests cover both the SQLAlchemy query paths and the time-series behavior:
- per-check latest result
- history with cutoff windows
- aggregate stats (count, avg, percentiles)
- bulk delete (retention)
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

_tests_root = str(Path(__file__).resolve().parent.parent)
if _tests_root not in sys.path:
    sys.path.insert(0, _tests_root)

from fixtures.factories import (  # noqa: E402
    make_agent,
    make_check,
    make_check_result,
    make_check_result_batch,
)

from app.core.datetime_utils import utc_now  # noqa: E402
from app.crud.check_result_crud import CheckResultCRUD  # noqa: E402

pytestmark = pytest.mark.integration


async def _agent_with_check(db, **check_overrides):
    a = make_agent()
    db.add(a)
    await db.flush()
    c = make_check(agent_id=a.id, **check_overrides)
    db.add(c)
    await db.flush()
    return a, c


# ---------------------------------------------------------------------------
# get_latest_per_check_for_agent
# ---------------------------------------------------------------------------


class TestGetLatestPerCheckForAgent:
    async def test_returns_one_row_per_check(self, db: AsyncSession):
        agent, check_a = await _agent_with_check(db)
        check_b = make_check(agent_id=agent.id, display_name="b")
        db.add(check_b)
        await db.flush()
        # 3 results for A, 2 for B — should get 1 row per check (latest)
        for r in make_check_result_batch(check_id=check_a.id, agent_id=agent.id, count=3):
            db.add(r)
        for r in make_check_result_batch(check_id=check_b.id, agent_id=agent.id, count=2):
            db.add(r)
        await db.flush()

        rows = await CheckResultCRUD.get_latest_per_check_for_agent(
            db, agent.id, utc_now() - timedelta(hours=24)
        )
        assert len(rows) == 2

    async def test_excludes_results_before_cutoff(self, db: AsyncSession):
        agent, check = await _agent_with_check(db)
        now = utc_now()
        # Old result outside window
        db.add(
            make_check_result(
                check_id=check.id,
                agent_id=agent.id,
                timestamp=now - timedelta(days=2),
            )
        )
        # Recent result inside window
        db.add(
            make_check_result(
                check_id=check.id,
                agent_id=agent.id,
                timestamp=now,
            )
        )
        await db.flush()

        rows = await CheckResultCRUD.get_latest_per_check_for_agent(
            db, agent.id, now - timedelta(hours=1)
        )
        assert len(rows) == 1
        # Newest one wins
        assert (now - rows[0].timestamp).total_seconds() < 60

    async def test_other_agent_results_excluded(self, db: AsyncSession):
        a1, c1 = await _agent_with_check(db)
        a2, c2 = await _agent_with_check(db)
        db.add(make_check_result(check_id=c1.id, agent_id=a1.id))
        db.add(make_check_result(check_id=c2.id, agent_id=a2.id))
        await db.flush()

        rows = await CheckResultCRUD.get_latest_per_check_for_agent(
            db, a1.id, utc_now() - timedelta(hours=1)
        )
        assert len(rows) == 1
        assert rows[0].agent_id == a1.id


# ---------------------------------------------------------------------------
# get_history_for_check
# ---------------------------------------------------------------------------


class TestGetHistoryForCheck:
    async def test_returns_newest_first(self, db: AsyncSession):
        agent, check = await _agent_with_check(db)
        for r in make_check_result_batch(check_id=check.id, agent_id=agent.id, count=5):
            db.add(r)
        await db.flush()

        rows = await CheckResultCRUD.get_history_for_check(
            db, check.id, utc_now() - timedelta(hours=24), limit=100
        )
        assert len(rows) == 5
        # Newest first
        for a, b in zip(rows, rows[1:], strict=False):
            assert a.timestamp >= b.timestamp

    async def test_respects_limit(self, db: AsyncSession):
        agent, check = await _agent_with_check(db)
        for r in make_check_result_batch(check_id=check.id, agent_id=agent.id, count=10):
            db.add(r)
        await db.flush()

        rows = await CheckResultCRUD.get_history_for_check(
            db, check.id, utc_now() - timedelta(hours=24), limit=3
        )
        assert len(rows) == 3

    async def test_respects_cutoff(self, db: AsyncSession):
        agent, check = await _agent_with_check(db)
        now = utc_now()
        # 2 inside, 2 outside
        for offset_min in (1, 5, 60, 120):
            db.add(
                make_check_result(
                    check_id=check.id,
                    agent_id=agent.id,
                    timestamp=now - timedelta(minutes=offset_min),
                )
            )
        await db.flush()

        rows = await CheckResultCRUD.get_history_for_check(
            db, check.id, now - timedelta(minutes=10), limit=100
        )
        assert len(rows) == 2  # only the 1-min and 5-min ones


# ---------------------------------------------------------------------------
# Aggregate stats
# ---------------------------------------------------------------------------


class TestSummaryStats:
    async def test_counts_and_averages(self, db: AsyncSession):
        agent, check = await _agent_with_check(db)
        # 3 success @ 100ms, 2 failure @ 500ms
        for _ in range(3):
            db.add(
                make_check_result(
                    check_id=check.id,
                    agent_id=agent.id,
                    success=True,
                    latency_ms=100.0,
                )
            )
        for _ in range(2):
            db.add(
                make_check_result(
                    check_id=check.id,
                    agent_id=agent.id,
                    success=False,
                    latency_ms=500.0,
                )
            )
        await db.flush()

        stats = await CheckResultCRUD.get_summary_stats_for_check(
            db, check.id, utc_now() - timedelta(hours=1)
        )
        assert stats.total_checks == 5
        assert stats.successful_checks == 3
        # avg = (3*100 + 2*500) / 5 = 260
        assert stats.avg_latency_ms == pytest.approx(260.0)
        assert stats.min_latency_ms == pytest.approx(100.0)
        assert stats.max_latency_ms == pytest.approx(500.0)

    async def test_empty_returns_nulls(self, db: AsyncSession):
        agent, check = await _agent_with_check(db)
        stats = await CheckResultCRUD.get_summary_stats_for_check(
            db, check.id, utc_now() - timedelta(hours=1)
        )
        assert stats.total_checks == 0
        # Aggregates over an empty set return None
        assert stats.avg_latency_ms is None


class TestLatencyPercentiles:
    async def test_computes_p50_p95_p99(self, db: AsyncSession):
        agent, check = await _agent_with_check(db)
        # Linear distribution 10..100ms
        for ms in range(10, 101, 10):  # 10, 20, ..., 100
            db.add(
                make_check_result(
                    check_id=check.id,
                    agent_id=agent.id,
                    latency_ms=float(ms),
                )
            )
        await db.flush()

        p = await CheckResultCRUD.get_latency_percentiles_for_check(
            db, check.id, utc_now() - timedelta(hours=1)
        )
        assert p is not None
        # p50 of 10..100 step 10 is 55 (interpolated)
        assert 50 <= p.p50 <= 60
        # p95 close to 95
        assert 90 <= p.p95 <= 100

    async def test_no_data_returns_null_row(self, db: AsyncSession):
        agent, check = await _agent_with_check(db)
        p = await CheckResultCRUD.get_latency_percentiles_for_check(
            db, check.id, utc_now() - timedelta(hours=1)
        )
        # Aggregate query returns one row of NULLs when no data
        assert p is None or p.p50 is None


class TestOverallStats:
    async def test_aggregates_across_all_checks(self, db: AsyncSession):
        agent1, check1 = await _agent_with_check(db)
        agent2, check2 = await _agent_with_check(db)
        db.add(
            make_check_result(
                check_id=check1.id, agent_id=agent1.id, success=True, latency_ms=100.0
            )
        )
        db.add(
            make_check_result(
                check_id=check2.id, agent_id=agent2.id, success=False, latency_ms=200.0
            )
        )
        await db.flush()

        stats = await CheckResultCRUD.get_overall_stats(db, utc_now() - timedelta(hours=1))
        assert stats.total_checks == 2
        assert stats.successful_checks == 1
        assert stats.avg_latency_ms == pytest.approx(150.0)


# ---------------------------------------------------------------------------
# Activity counters
# ---------------------------------------------------------------------------


class TestActivityCounters:
    async def test_count_active_agents_since(self, db: AsyncSession):
        now = utc_now()
        fresh = make_agent(last_seen=now)
        stale = make_agent(last_seen=now - timedelta(hours=5))
        db.add(fresh)
        db.add(stale)
        await db.flush()

        count = await CheckResultCRUD.count_active_agents_since(db, now - timedelta(hours=1))
        assert count == 1

    async def test_count_active_checks_since(self, db: AsyncSession):
        now = utc_now()
        fresh_agent = make_agent(last_seen=now)
        stale_agent = make_agent(last_seen=now - timedelta(hours=5))
        db.add(fresh_agent)
        db.add(stale_agent)
        await db.flush()
        db.add(make_check(agent_id=fresh_agent.id))
        db.add(make_check(agent_id=fresh_agent.id))
        db.add(make_check(agent_id=stale_agent.id))
        await db.flush()

        count = await CheckResultCRUD.count_active_checks_since(db, now - timedelta(hours=1))
        assert count == 2

    async def test_count_since(self, db: AsyncSession):
        agent, check = await _agent_with_check(db)
        now = utc_now()
        # Distinct timestamps per (check) — uq_check_results_check_ts forbids
        # two results for the same check at the same instant.
        db.add(make_check_result(check_id=check.id, agent_id=agent.id, timestamp=now))
        db.add(
            make_check_result(
                check_id=check.id,
                agent_id=agent.id,
                timestamp=now - timedelta(minutes=1),
            )
        )
        db.add(
            make_check_result(
                check_id=check.id,
                agent_id=agent.id,
                timestamp=now - timedelta(days=2),
            )
        )
        await db.flush()

        count = await CheckResultCRUD.count_since(db, now - timedelta(hours=1))
        assert count == 2


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


class TestDeleteOlderThan:
    async def test_deletes_old_results(self, db: AsyncSession):
        agent, check = await _agent_with_check(db)
        now = utc_now()
        # 2 keep, 3 drop — distinct timestamps (uq_check_results_check_ts).
        for i in range(2):
            db.add(
                make_check_result(
                    check_id=check.id,
                    agent_id=agent.id,
                    timestamp=now - timedelta(minutes=i),
                )
            )
        for i in range(3):
            db.add(
                make_check_result(
                    check_id=check.id,
                    agent_id=agent.id,
                    timestamp=now - timedelta(days=10, minutes=i),
                )
            )
        await db.flush()

        deleted = await CheckResultCRUD.delete_older_than(db, now - timedelta(days=1))
        assert deleted == 3

        remaining = await CheckResultCRUD.count_since(db, now - timedelta(days=30))
        assert remaining == 2


# ---------------------------------------------------------------------------
# Success stats
# ---------------------------------------------------------------------------


class TestSuccessStats:
    async def test_returns_success_and_total_for_check(self, db: AsyncSession):
        agent, check = await _agent_with_check(db)
        now = utc_now()
        # 4 success, 1 failure
        for _ in range(4):
            db.add(
                make_check_result(
                    check_id=check.id,
                    agent_id=agent.id,
                    success=True,
                )
            )
        db.add(
            make_check_result(
                check_id=check.id,
                agent_id=agent.id,
                success=False,
            )
        )
        await db.flush()

        total, successful = await CheckResultCRUD.get_success_stats_for_check(
            db, check.id, now - timedelta(hours=1)
        )
        assert total == 5
        assert successful == 4
