"""Whether a check is in an outage, as its alerts judge it.

A check is down while any agent that currently runs it has a failing latest
result, and up once every such agent's latest result succeeds. A latency
threshold is breached while any such agent's latest latency meets it, and clear
once every one is on the safe side; a result with no latency (a timeout) is
passed over, so it neither starts nor ends a breach.

An agent's latest result counts while it is recent (`lookback`): an agent that
stopped reporting does not hold the check's state, the silence alerts tell of
it. A check one agent runs is judged by that agent's results, as before.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.alert_crud import AlertCRUD
from app.models.check_model import Check
from app.models.check_result_model import CheckResult
from app.services.core.agent_assignment_core_service import AgentAssignmentCoreService

# How far back an agent's latest result still speaks for the check, in check
# intervals, and at least this long.
LOOKBACK_INTERVALS = 3
LOOKBACK_MINIMUM = timedelta(minutes=5)
DEFAULT_INTERVAL_SECONDS = 60
THRESHOLD_OPERATORS = (">", ">=", "<", "<=", "==")


def threshold_value(config: dict[str, Any] | None) -> float | None:
    """A latency threshold's value as a number, or None when it has none it
    can compare (another metric, no value, not a number)."""
    config = config or {}
    if config.get("metric", "latency_ms") != "latency_ms":
        return None
    value = config.get("value")
    # trigger_config is free-form JSON: a number or a numeric string compares.
    if value is None or not isinstance(value, int | float | str):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def latency_breaches(latency_ms: float | None, config: dict[str, Any] | None) -> bool:
    """Whether `latency_ms` meets the threshold's comparison (e.g. > 1000)."""
    threshold = threshold_value(config)
    if threshold is None or latency_ms is None:
        return False
    current = float(latency_ms)
    operator = (config or {}).get("operator", ">")
    if operator == ">":
        return current > threshold
    if operator == ">=":
        return current >= threshold
    if operator == "<":
        return current < threshold
    if operator == "<=":
        return current <= threshold
    if operator == "==":
        return current == threshold
    return False


@dataclass(frozen=True)
class CheckView:
    """A check as its alerts see it at one result: each running agent's latest
    result (`results`) and latest result with a latency (`latencies`)."""

    results: Sequence[CheckResult]
    latencies: Sequence[CheckResult]

    @staticmethod
    def of(result: CheckResult) -> CheckView:
        """The view of a check one result speaks for alone."""
        return CheckView(
            results=[result],
            latencies=[result] if result.latency_ms is not None else [],
        )

    @property
    def down(self) -> bool:
        """Whether any running agent's latest result fails."""
        return any(not result.success for result in self.results)

    @property
    def up(self) -> bool:
        """Whether every running agent's latest result succeeds."""
        return bool(self.results) and not self.down

    def breached(self, config: dict[str, Any] | None) -> bool:
        """Whether any running agent's latest latency meets the threshold."""
        return any(
            latency_breaches(result.latency_ms, config) for result in self.latencies
        )

    def clear(self, config: dict[str, Any] | None) -> bool:
        """Whether every running agent's latest latency is on the safe side."""
        return (
            threshold_value(config) is not None
            and bool(self.latencies)
            and not self.breached(config)
        )


class CheckOutageCoreService:
    """Reads a check's state across the agents that run it."""

    @staticmethod
    def lookback(check: Check) -> timedelta:
        """How long an agent's latest result still speaks for `check`."""
        interval = timedelta(seconds=check.interval_seconds or DEFAULT_INTERVAL_SECONDS)
        return max(interval * LOOKBACK_INTERVALS, LOOKBACK_MINIMUM)

    @staticmethod
    async def view(db: AsyncSession, check: Check, result: CheckResult) -> CheckView:
        """The check at `result`: the latest results, up to it, of the agents
        that run the check now. `result` alone when none of them has one (its
        agent no longer runs the check, say, and nothing else reported)."""
        agents = await AgentAssignmentCoreService.agents_running(db, check)
        if agents == [result.agent_id]:
            # One agent: its own newest latency may predate this result.
            latencies = (
                [result]
                if result.latency_ms is not None
                else await AlertCRUD.latest_results_by_agent(
                    db,
                    check.id,
                    agents,
                    since=result.timestamp - CheckOutageCoreService.lookback(check),
                    at_or_before=result.timestamp,
                    with_latency=True,
                )
            )
            return CheckView(results=[result], latencies=latencies)
        since = result.timestamp - CheckOutageCoreService.lookback(check)
        results = await AlertCRUD.latest_results_by_agent(
            db, check.id, agents, since=since, at_or_before=result.timestamp
        )
        if not results:
            return CheckView.of(result)
        latencies = await AlertCRUD.latest_results_by_agent(
            db,
            check.id,
            agents,
            since=since,
            at_or_before=result.timestamp,
            with_latency=True,
        )
        return CheckView(results=results, latencies=latencies)
