"""Whether a check is in an outage, as its alerts judge it.

A check is down while any agent that currently runs it has a failing latest
result, and up once every such agent's latest result succeeds. A latency
threshold is breached while any such agent's latest latency meets it, and clear
once every one is on the safe side; a result with no latency (a timeout) is
passed over, so it neither starts nor ends a breach.

Each running agent's latest result counts however old it is: an agent that
stopped reporting keeps the check as it last saw it (the silence alerts tell of
the silence). A check one agent runs is judged by that agent's results.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.alert_crud import AlertCRUD
from app.models.check_model import Check
from app.models.check_result_model import CheckResult
from app.services.core.agent_assignment_core_service import AgentAssignmentCoreService

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

    def worst(self, config: dict[str, Any] | None) -> CheckResult | None:
        """The latest result whose latency breaches the threshold most, or
        None while none does: what a breach notification tells of."""
        breaching = [
            result
            for result in self.latencies
            if latency_breaches(result.latency_ms, config)
        ]
        operator = (config or {}).get("operator", ">")
        if not breaching:
            return None
        if operator in ("<", "<="):
            return min(breaching, key=lambda r: float(r.latency_ms or 0))
        return max(breaching, key=lambda r: float(r.latency_ms or 0))

    def newest_clear(self, config: dict[str, Any] | None) -> CheckResult | None:
        """The newest latest result whose latency is on the safe side: what a
        latency recovery tells of."""
        safe = [
            result
            for result in self.latencies
            if not latency_breaches(result.latency_ms, config)
        ]
        return max(safe, key=lambda r: r.timestamp) if safe else None

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
    async def view(db: AsyncSession, check: Check, result: CheckResult) -> CheckView:
        """The check at `result`: the latest results, up to it, of the agents
        that run the check now, and of `result`'s own agent (a late batch from
        an agent the check has since moved off still speaks for itself)."""
        agents = list(
            dict.fromkeys(
                [
                    *await AgentAssignmentCoreService.agents_running(db, check),
                    result.agent_id,
                ]
            )
        )
        if agents == [result.agent_id]:
            # One agent: its own newest latency may predate this result.
            latencies = (
                [result]
                if result.latency_ms is not None
                else await AlertCRUD.latest_results_by_agent(
                    db,
                    check.id,
                    agents,
                    at_or_before=result.timestamp,
                    with_latency=True,
                )
            )
            return CheckView(results=[result], latencies=latencies)
        results = await AlertCRUD.latest_results_by_agent(
            db, check.id, agents, at_or_before=result.timestamp
        )
        latencies = await AlertCRUD.latest_results_by_agent(
            db, check.id, agents, at_or_before=result.timestamp, with_latency=True
        )
        return CheckView(results=results or [result], latencies=latencies)
