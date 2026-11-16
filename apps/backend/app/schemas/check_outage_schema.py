"""A check as its status and threshold alerts see it at one result."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.core.latency_threshold import latency_breaches, threshold_value
from app.models.check_result_model import CheckResult


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
