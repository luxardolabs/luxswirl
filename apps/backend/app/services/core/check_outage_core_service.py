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

from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.alert_crud import AlertCRUD
from app.models.check_model import Check
from app.models.check_result_model import CheckResult
from app.schemas.check_outage_schema import CheckView
from app.services.core.agent_assignment_core_service import AgentAssignmentCoreService


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
