"""
Check result API core service - check results in their JSON API shape.

One builder makes every CheckResultResponse, so the latest-results list and a
check's history report a result the same way. A check's history is bound to the
agent named in its path: a check under another agent is not found there
(LUXSWIRL-190 L-5).
"""

from datetime import timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_utils import utc_now
from app.core.exceptions import CheckNotFoundException
from app.models.check_result_model import CheckResult
from app.schemas.check_result_schema import (
    CheckHistoryResponse,
    CheckResultListResponse,
    CheckResultResponse,
)
from app.services.core.check_core_service import CheckCoreService
from app.services.core.check_result_core_service import CheckResultCoreService


class CheckResultApiCoreService:
    """Check results for the JSON API."""

    @staticmethod
    def to_response(result: CheckResult) -> CheckResultResponse:
        """`result` in its API shape, labelled with its agent and check."""
        check = result.check
        return CheckResultResponse(
            id=result.id,
            agent_id=result.agent_id,
            check_id=result.check_id,
            timestamp=result.timestamp,
            success=result.success,
            latency_ms=result.latency_ms,
            latency_seconds=result.latency_seconds,
            error=result.error,
            error_type=result.error_type,
            http_status_code=result.http_status_code,
            http_response_time_ms=result.http_response_time_ms,
            metrics=result.get_metrics(),
            status=result.status,
            agent_name=result.agent.agent_name if result.agent else None,
            check_name=check.display_name if check else None,
            check_type=check.check_type if check else None,
            target=check.target if check else None,
        )

    @staticmethod
    async def latest_results_for_agent(
        db: AsyncSession, agent_name: str, minutes: int
    ) -> CheckResultListResponse:
        """Each of the agent's checks' latest result in the last `minutes`.

        Raises:
            AgentNotFoundException: If no agent has that name
        """
        results = await CheckResultCoreService.get_latest_results_for_agent(
            db, agent_name, minutes
        )
        responses = [CheckResultApiCoreService.to_response(r) for r in results]
        total = len(responses)
        successes = sum(1 for r in responses if r.success)
        return CheckResultListResponse(
            results=responses,
            total=total,
            success_count=successes,
            failure_count=total - successes,
            success_rate=round(successes / total * 100, 2) if total else 0.0,
        )

    @staticmethod
    async def check_history(
        db: AsyncSession, agent_name: str, check_id: UUID, hours: int, limit: int
    ) -> CheckHistoryResponse:
        """A check's results over the last `hours`, with summary statistics.

        Raises:
            CheckNotFoundException: If the check does not belong to the agent
                named `agent_name`, or has no results in the window
        """
        check = await CheckCoreService.get_check_by_id(db, check_id)
        if check is None or agent_name not in (
            check.agent.agent_name,
            str(check.agent_id),
        ):
            raise CheckNotFoundException(agent_name, str(check_id))
        results = await CheckResultCoreService.get_check_history(
            db, check_id, hours, limit
        )
        if not results:
            raise CheckNotFoundException(agent_name, str(check_id))
        end_time = utc_now()
        return CheckHistoryResponse(
            agent_id=check.agent_id,
            check_name=check.display_name,
            check_type=check.check_type,
            target=check.target,
            start_time=end_time - timedelta(hours=hours),
            end_time=end_time,
            data_points=[CheckResultApiCoreService.to_response(r) for r in results],
            summary=await CheckResultCoreService.get_check_summary(db, check_id, hours),
        )
