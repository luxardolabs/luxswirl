"""The batched reads must answer exactly what the per-row reads answered.

Each of these replaced a loop that issued one query per agent, per check or per
default setting. Collapsing a loop into one round-trip is only a win if the
answer is identical, so every test here pins the batched form against the
single-row form it replaced — an equivalence oracle, not a query count, because
a query count would pass on a batched query that returns the wrong rows.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.core.datetime_utils import utc_now
from app.crud.agent_crud import AgentCRUD
from app.crud.alert_crud import AlertCRUD
from app.crud.check_result_crud import CheckResultCRUD
from app.crud.setting_crud import SettingCRUD
from app.services.core.settings_core_service import SettingsCoreService
from fixtures.factories import (
    make_agent,
    make_alert,
    make_alert_check_mapping,
    make_check,
    make_check_result,
)
from sqlalchemy.ext.asyncio import AsyncSession


class TestAgentHostnames:
    async def test_matches_the_per_agent_read(self, db: AsyncSession):
        a1, a2 = make_agent(), make_agent()
        db.add_all([a1, a2])
        await db.flush()

        hostnames = await AgentCRUD.get_hostnames_by_ids(db, [a1.id, a2.id])

        assert hostnames == {a1.id: a1.hostname, a2.id: a2.hostname}

    async def test_unknown_id_is_absent_not_none(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        missing = make_agent().id

        hostnames = await AgentCRUD.get_hostnames_by_ids(db, [agent.id, missing])

        # The caller does `.get(id)` and expects None for a gone agent; an
        # explicit None entry would be indistinguishable from a null hostname.
        assert missing not in hostnames
        assert hostnames[agent.id] == agent.hostname

    async def test_empty_id_list_returns_empty(self, db: AsyncSession):
        assert await AgentCRUD.get_hostnames_by_ids(db, []) == {}


class TestLatestResultsAcrossAgents:
    async def test_matches_the_union_of_per_agent_reads(self, db: AsyncSession):
        now = utc_now()
        cutoff = now - timedelta(hours=1)
        agents = [make_agent() for _ in range(3)]
        db.add_all(agents)
        await db.flush()

        checks = []
        for agent in agents[:2]:  # the third agent reports nothing
            check = make_check(agent_id=agent.id)
            db.add(check)
            await db.flush()
            checks.append(check)
            db.add(
                make_check_result(
                    check_id=check.id,
                    agent_id=agent.id,
                    timestamp=now - timedelta(minutes=30),
                    success=False,
                )
            )
            db.add(
                make_check_result(
                    check_id=check.id,
                    agent_id=agent.id,
                    timestamp=now - timedelta(minutes=5),
                    success=True,
                )
            )
        await db.flush()

        batched = await CheckResultCRUD.get_latest_results_for_agents_with_check(
            db, [a.id for a in agents], cutoff
        )

        per_agent = []
        for agent in agents:
            per_agent += await CheckResultCRUD.get_latest_results_for_agent_with_check(
                db, agent.id, cutoff
            )

        def key(rows):
            return sorted((r.check_id, r.timestamp, c.id) for r, c in rows)

        assert key(batched) == key(per_agent)
        # Latest-per-check, so the older failure must not come back.
        assert all(result.success for result, _ in batched)

    async def test_excludes_results_older_than_the_cutoff(self, db: AsyncSession):
        now = utc_now()
        agent = make_agent()
        db.add(agent)
        await db.flush()
        check = make_check(agent_id=agent.id)
        db.add(check)
        await db.flush()
        db.add(
            make_check_result(
                check_id=check.id,
                agent_id=agent.id,
                timestamp=now - timedelta(hours=5),
            )
        )
        await db.flush()

        rows = await CheckResultCRUD.get_latest_results_for_agents_with_check(
            db, [agent.id], now - timedelta(hours=1)
        )

        assert rows == []

    async def test_empty_agent_list_returns_empty(self, db: AsyncSession):
        assert (
            await CheckResultCRUD.get_latest_results_for_agents_with_check(
                db, [], utc_now()
            )
            == []
        )


class TestAlertIdsForChecks:
    async def test_matches_the_per_check_read(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        c1, c2, c3 = (make_check(agent_id=agent.id) for _ in range(3))
        alert_a, alert_b = make_alert(), make_alert()
        db.add_all([c1, c2, c3, alert_a, alert_b])
        await db.flush()

        db.add_all(
            [
                make_alert_check_mapping(alert_id=alert_a.id, check_id=c1.id),
                make_alert_check_mapping(alert_id=alert_b.id, check_id=c1.id),
                make_alert_check_mapping(alert_id=alert_a.id, check_id=c2.id),
            ]
        )
        await db.flush()

        batched = await AlertCRUD.list_alert_ids_for_checks(db, [c1.id, c2.id, c3.id])

        for check in (c1, c2, c3):
            single = set(await AlertCRUD.list_alert_ids_for_check(db, check.id))
            assert batched.get(check.id, set()) == single

        # A check with no alerts is absent, so `.get(id, ())` yields empty.
        assert c3.id not in batched

    async def test_empty_check_list_returns_empty(self, db: AsyncSession):
        assert await AlertCRUD.list_alert_ids_for_checks(db, []) == {}


class TestExistingSettingKeys:
    async def test_returns_every_stored_key(self, db: AsyncSession):
        await SettingsCoreService.create_setting(
            db, key="batched.a", category="system", value=1, display_name="A"
        )
        await SettingsCoreService.create_setting(
            db, key="batched.b", category="system", value=2, display_name="B"
        )

        keys = await SettingCRUD.existing_keys(db)

        assert {"batched.a", "batched.b"} <= keys
        assert "batched.never-created" not in keys

    async def test_seeding_twice_creates_nothing_the_second_time(
        self, db: AsyncSession
    ):
        """ensure_default_settings reads the key set once and must stay
        idempotent — the whole point of the read it replaced."""
        await SettingsCoreService.ensure_default_settings(db)
        after_first = await SettingCRUD.existing_keys(db)

        await SettingsCoreService.ensure_default_settings(db)

        assert await SettingCRUD.existing_keys(db) == after_first
