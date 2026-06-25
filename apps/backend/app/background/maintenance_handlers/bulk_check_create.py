"""Handler for bulk_check_create maintenance jobs.

job.params expects {
    "agent_id": "uuid",
    "requests": [BulkCheckCreateRequest dict, ...],
    "alert_ids": ["uuid", ...] | [],
}.
"""

from uuid import UUID

from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.maintenance_job_model import MaintenanceJob
from app.schemas.check_schema import BulkCheckCreateRequest
from app.services.core.alert_core_service import AlertCoreService
from app.services.views.checks_view_service import ChecksViewService

logger = get_logger("luxswirl.maintenance.bulk_check_create")


async def handle(db: AsyncSession, job: MaintenanceJob) -> None:
    agent_id_raw = job.params.get("agent_id")
    raw_requests = job.params.get("requests") or []
    alert_ids = job.params.get("alert_ids") or []

    if not agent_id_raw:
        raise ValueError("bulk_check_create job requires params.agent_id")
    if not raw_requests:
        logger.info("bulk_check_create with empty requests — nothing to do")
        return

    agent_id = UUID(agent_id_raw) if isinstance(agent_id_raw, str) else agent_id_raw
    requests = [BulkCheckCreateRequest(**r) for r in raw_requests]

    logger.info(
        "Bulk creating checks",
        extra={
            "agent_id": str(agent_id),
            "count": len(requests),
            "job_id": str(job.id),
        },
    )

    result = await ChecksViewService.bulk_create_checks(db, agent_id, requests)

    if alert_ids and result.results:
        check_ids = [r.check_id for r in result.results if r.check_id]
        for alert_id_str in alert_ids:
            if alert_id_str:
                await AlertCoreService.bulk_assign_to_checks(db, UUID(alert_id_str), check_ids)

    logger.info(
        "Bulk create complete",
        extra={
            "succeeded": result.succeeded,
            "failed": result.failed,
            "job_id": str(job.id),
        },
    )
