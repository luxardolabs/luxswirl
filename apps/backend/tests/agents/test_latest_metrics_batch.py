"""The fleet-wide latest-metric read must match the per-agent one.

The Prometheus rebuild used to issue one query per agent. The batched form has
to return exactly what the single-agent read would have, or a scrape quietly
reports the wrong sample.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.datetime_utils import utc_now
from app.crud.agent_crud import AgentCRUD
from app.models.agent_metric_model import AgentMetric
from fixtures.factories import make_agent
from sqlalchemy.ext.asyncio import AsyncSession


async def _metric(db: AsyncSession, agent_id: UUID, *, minutes_ago: int, cpu: float):
    metric = AgentMetric(
        agent_id=agent_id,
        timestamp=utc_now() - timedelta(minutes=minutes_ago),
        cpu_percent=cpu,
    )
    db.add(metric)
    await db.flush()
    return metric


async def test_batch_matches_per_agent_reads(db: AsyncSession):
    a1, a2, a3 = make_agent(), make_agent(), make_agent()
    db.add_all([a1, a2, a3])
    await db.flush()

    await _metric(db, a1.id, minutes_ago=10, cpu=1.0)
    newest_a1 = await _metric(db, a1.id, minutes_ago=1, cpu=2.0)
    newest_a2 = await _metric(db, a2.id, minutes_ago=5, cpu=3.0)

    batched = await AgentCRUD.get_latest_metrics(db, [a1.id, a2.id, a3.id])

    assert batched[a1.id].timestamp == newest_a1.timestamp
    assert batched[a2.id].timestamp == newest_a2.timestamp
    # An agent that has never reported is absent, not present-and-None.
    assert a3.id not in batched

    for agent_id in (a1.id, a2.id, a3.id):
        single = await AgentCRUD.get_latest_metric(db, agent_id)
        expected = single.timestamp if single else None
        got = batched[agent_id].timestamp if agent_id in batched else None
        assert got == expected


async def test_empty_id_list_does_not_query(db: AsyncSession):
    assert await AgentCRUD.get_latest_metrics(db, []) == {}
