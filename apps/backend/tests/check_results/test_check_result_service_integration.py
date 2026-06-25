"""Integration tests for CheckResultCoreService.

Focus: `process_agent_report` — the agent ingestion path. This is the
single highest-leverage method in the backend; every check result the
system stores flows through it. Untested before LUXSWIRL-127.
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

_tests_root = str(Path(__file__).resolve().parent.parent)
if _tests_root not in sys.path:
    sys.path.insert(0, _tests_root)

from fixtures.factories import make_agent, make_check  # noqa: E402

from app.core.datetime_utils import utc_now  # noqa: E402
from app.core.exceptions import AgentNotFoundException  # noqa: E402
from app.crud.check_result_crud import CheckResultCRUD  # noqa: E402
from app.schemas.check_result_schema import (  # noqa: E402
    AgentReportRequest,
    CheckResultCreate,
)
from app.services.core.check_result_core_service import CheckResultCoreService  # noqa: E402

pytestmark = pytest.mark.integration


def _result_payload(*, check_id, **overrides) -> CheckResultCreate:
    defaults = {
        "timestamp": utc_now(),
        "success": True,
        "latency_ms": 12.5,
        "check_id": str(check_id),
        "display_name": "test-check",
        "check_type": "ping",
        "target": "127.0.0.1",
    }
    defaults.update(overrides)
    return CheckResultCreate(**defaults)


class TestReportBounds:
    """M-4 (LUXSWIRL-190): the agent report payload is bounded to prevent an
    unbounded-write DoS from a malicious/compromised agent."""

    def test_too_many_checks_rejected(self):
        item = {
            "timestamp": utc_now(),
            "success": True,
            "display_name": "x",
            "check_type": "ping",
            "target": "x",
        }
        with pytest.raises(ValidationError):  # checks is capped at 10000
            AgentReportRequest(agent_id=uuid4(), checks=[item] * 10001)

    def test_oversized_target_rejected(self):
        with pytest.raises(ValidationError):  # target max_length=512
            _result_payload(check_id=uuid4(), target="x" * 600)

    def test_oversized_response_data_rejected(self):
        with pytest.raises(ValidationError):  # response_data max_length=65535
            _result_payload(check_id=uuid4(), response_data="x" * 70000)


# ---------------------------------------------------------------------------
# process_agent_report — happy path + edge cases
# ---------------------------------------------------------------------------


class TestProcessAgentReport:
    async def test_ingests_results_for_known_check(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        check = make_check(agent_id=agent.id)
        db.add(check)
        await db.flush()

        report = AgentReportRequest(
            agent_id=agent.id,
            checks=[
                _result_payload(check_id=check.id, success=True, latency_ms=10.0),
                _result_payload(
                    check_id=check.id, success=False, latency_ms=200.0, error="timeout"
                ),
            ],
        )
        result = await CheckResultCoreService.process_agent_report(db, report)

        assert result["results_processed"] == 2
        assert result["results_failed"] == 0

        # Verify rows landed in the hypertable
        history = await CheckResultCRUD.get_history_for_check(
            db,
            check.id,
            utc_now() - __import__("datetime").timedelta(hours=1),
            limit=10,
        )
        assert len(history) == 2

    async def test_unknown_check_id_skipped(self, db: AsyncSession):
        """Checks not registered with the agent are skipped (results_failed)."""
        agent = make_agent()
        db.add(agent)
        await db.flush()

        report = AgentReportRequest(
            agent_id=agent.id,
            checks=[_result_payload(check_id=uuid4(), display_name="unregistered")],
        )
        result = await CheckResultCoreService.process_agent_report(db, report)
        assert result["results_processed"] == 0
        assert result["results_failed"] == 1

    async def test_internal_check_type_silently_dropped(self, db: AsyncSession):
        """Agent-health internal checks should go through /heartbeat, not the
        report endpoint. process_agent_report drops them with a warning."""
        agent = make_agent()
        db.add(agent)
        await db.flush()

        report = AgentReportRequest(
            agent_id=agent.id,
            checks=[
                _result_payload(
                    check_id=uuid4(),
                    check_type="internal",
                    display_name="agent_health",
                ),
            ],
        )
        result = await CheckResultCoreService.process_agent_report(db, report)
        assert result["results_processed"] == 0
        assert result["results_failed"] == 1

    async def test_missing_agent_raises(self, db: AsyncSession):
        report = AgentReportRequest(
            agent_id=uuid4(),
            checks=[
                _result_payload(check_id=uuid4(), display_name="x"),
            ],
        )
        with pytest.raises(AgentNotFoundException):
            await CheckResultCoreService.process_agent_report(db, report)

    async def test_updates_agent_last_seen(self, db: AsyncSession):
        """Every report bumps the agent's last_seen so the dashboard knows
        the agent is alive."""
        import datetime as dt

        agent = make_agent(last_seen=utc_now() - dt.timedelta(hours=2))
        db.add(agent)
        await db.flush()
        check = make_check(agent_id=agent.id)
        db.add(check)
        await db.flush()
        original_last_seen = agent.last_seen

        report = AgentReportRequest(
            agent_id=agent.id,
            checks=[_result_payload(check_id=check.id)],
        )
        await CheckResultCoreService.process_agent_report(db, report)

        await db.refresh(agent)
        assert agent.last_seen > original_last_seen

    async def test_uses_per_check_timestamp(self, db: AsyncSession):
        """Each check_data has its own timestamp; results must use that, not
        the report's batch timestamp."""
        import datetime as dt

        agent = make_agent()
        db.add(agent)
        await db.flush()
        check = make_check(agent_id=agent.id)
        db.add(check)
        await db.flush()

        per_check_time = utc_now() - dt.timedelta(minutes=5)
        report = AgentReportRequest(
            agent_id=agent.id,
            timestamp=utc_now(),  # batch ts is "now"
            checks=[
                _result_payload(
                    check_id=check.id,
                    timestamp=per_check_time,
                    success=True,
                ),
            ],
        )
        await CheckResultCoreService.process_agent_report(db, report)

        history = await CheckResultCRUD.get_history_for_check(
            db,
            check.id,
            utc_now() - dt.timedelta(hours=1),
            limit=10,
        )
        assert len(history) == 1
        # Per-check timestamp was preserved, not overwritten by batch ts
        assert abs((history[0].timestamp - per_check_time).total_seconds()) < 1

    async def test_invalid_result_id_falls_back_to_generated_uuid(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()
        check = make_check(agent_id=agent.id)
        db.add(check)
        await db.flush()

        report = AgentReportRequest(
            agent_id=agent.id,
            checks=[
                _result_payload(
                    check_id=check.id,
                    result_id="this-is-not-a-uuid",
                ),
            ],
        )
        # Bad result_id should not crash the whole report — falls back
        result = await CheckResultCoreService.process_agent_report(db, report)
        assert result["results_processed"] == 1

    async def test_metrics_with_ssl_certificate_data_persisted(self, db: AsyncSession):
        """When the agent reports SSL cert info, the service enriches it via
        compute_ssl_cert_info and persists the augmented dict."""
        import datetime as dt

        agent = make_agent()
        db.add(agent)
        await db.flush()
        check = make_check(agent_id=agent.id, check_type="http")
        db.add(check)
        await db.flush()

        # compute_ssl_cert_info parses with %b %d %H:%M:%S %Y %Z (the format
        # openssl emits), not ISO 8601. Format it accordingly.
        expiry = utc_now() + dt.timedelta(days=60)
        raw_cert = {
            "expiration_date": expiry.strftime("%b %d %H:%M:%S %Y GMT"),
            "subject": "*.example.test",
            "issuer": "Test CA",
        }
        report = AgentReportRequest(
            agent_id=agent.id,
            checks=[
                _result_payload(
                    check_id=check.id,
                    check_type="http",
                    metrics={"response": {"ssl_certificate": raw_cert}},
                ),
            ],
        )
        result = await CheckResultCoreService.process_agent_report(db, report)
        assert result["results_processed"] == 1

        history = await CheckResultCRUD.get_history_for_check(
            db,
            check.id,
            utc_now() - dt.timedelta(hours=1),
            limit=1,
        )
        assert len(history) == 1
        # Metrics serialized as JSON; the SSL block should be present
        import json as json_lib

        stored = json_lib.loads(history[0].metrics)
        assert "ssl_certificate" in stored["response"]
        # compute_ssl_cert_info adds days_until_expiration
        assert "days_until_expiration" in stored["response"]["ssl_certificate"]

    async def test_empty_checks_list_returns_zero(self, db: AsyncSession):
        agent = make_agent()
        db.add(agent)
        await db.flush()

        report = AgentReportRequest(agent_id=agent.id, checks=[])
        result = await CheckResultCoreService.process_agent_report(db, report)
        assert result["results_processed"] == 0
        assert result["results_failed"] == 0
