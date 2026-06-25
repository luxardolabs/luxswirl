"""Pure tests for the scheduler's decision logic (LUXSWIRL-127).

The coverage number was a lie of omission: scheduler_core sat at 63% with its two
most dangerous pieces uncovered —

  1. `_calculate_next_run` — the SCHEDULING MATH. A wrong cron/interval branch
     fires jobs at the wrong time or never reschedules them.
  2. `_scheduler_loop`'s exception handler — the CRASH-SURVIVAL guard. If a poll
     iteration raises and the loop doesn't swallow it, the scheduler dies
     silently and nothing ever runs again. That is exactly LUXSWIRL-191.

Both are pure (no DB) and cheap. They are the lines that actually matter, so they
get tested regardless of what the coverage percentage already "looked like".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.models.enum_model import SchedulerJobCategory, SchedulerTriggerType
from app.models.scheduler_model import JobConfiguration
from app.services.core.scheduler_core_service import SchedulerCoreService

pytestmark = pytest.mark.pure

_SVC = SchedulerCoreService()


def _job(**kw) -> JobConfiguration:
    # Column defaults do NOT apply to unpersisted ORM objects, so set every field
    # _calculate_next_run reads explicitly — notably jitter_ms, or `jitter_ms > 0`
    # compares None > 0 and raises.
    base: dict = {
        "job_key": "k",
        "function_name": "f",
        "display_name": "d",
        "category": SchedulerJobCategory.MONITORING,
        "trigger_type": SchedulerTriggerType.INTERVAL,
        "jitter_ms": 0,
        "timezone": "UTC",
    }
    base.update(kw)
    return JobConfiguration(**base)


class TestNextRunSchedulingMath:
    def test_interval_advances_to_the_future(self):
        job = _job(interval_seconds=60, next_run_at=datetime(2000, 1, 1, tzinfo=UTC))
        now = datetime.now(UTC)
        nxt = _SVC._calculate_next_run(job)
        assert now < nxt <= now + timedelta(seconds=60)

    def test_interval_with_no_base_is_now_plus_interval(self):
        job = _job(interval_seconds=300, next_run_at=None)
        delta = _SVC._calculate_next_run(job) - datetime.now(UTC)
        assert timedelta(seconds=298) <= delta <= timedelta(seconds=300)

    def test_cron_hourly_lands_on_the_minute(self):
        nxt = _SVC._calculate_next_run(
            _job(trigger_type=SchedulerTriggerType.CRON, cron_expression="30 * * * *")
        )
        assert (nxt.minute, nxt.second) == (30, 0)
        assert nxt > datetime.now(UTC)

    def test_cron_daily_lands_on_hour_and_minute(self):
        nxt = _SVC._calculate_next_run(
            _job(trigger_type=SchedulerTriggerType.CRON, cron_expression="0 2 * * *")
        )
        assert (nxt.hour, nxt.minute) == (2, 0)
        assert nxt > datetime.now(UTC)

    def test_cron_is_interpreted_in_the_jobs_timezone(self):
        nxt = _SVC._calculate_next_run(
            _job(
                trigger_type=SchedulerTriggerType.CRON,
                cron_expression="0 2 * * *",
                timezone="America/New_York",
            )
        )
        local = nxt.astimezone(ZoneInfo("America/New_York"))
        assert (local.hour, local.minute) == (2, 0)

    def test_malformed_cron_falls_back_to_tomorrow(self):
        nxt = _SVC._calculate_next_run(
            _job(trigger_type=SchedulerTriggerType.CRON, cron_expression="garbage")
        )
        assert nxt > datetime.now(UTC) + timedelta(hours=23)

    def test_manual_never_reschedules(self):
        assert _SVC._calculate_next_run(_job(trigger_type=SchedulerTriggerType.MANUAL)) is None


class TestPollLoopSurvivesABadIteration:
    """The exact LUXSWIRL-191 failure shape: a poll iteration raises. If the loop
    doesn't swallow it, the scheduler task dies and nothing ever runs again."""

    async def test_loop_catches_iteration_error_instead_of_dying(self, monkeypatch):
        svc = SchedulerCoreService()
        calls: list[int] = []

        async def boom():
            calls.append(1)
            raise RuntimeError("bad poll")

        async def stop_on_backoff(_secs):
            svc._running = False  # break the loop on the error-backoff sleep

        monkeypatch.setattr(svc, "_process_due_jobs", boom)
        monkeypatch.setattr(
            "app.services.core.scheduler_core_service.asyncio.sleep", stop_on_backoff
        )
        svc._running = True

        # Must return cleanly. A propagated exception here == a dead scheduler.
        await svc._scheduler_loop()
        assert calls == [1]
