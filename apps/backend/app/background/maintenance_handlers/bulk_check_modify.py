"""Handler for bulk_check_modify maintenance jobs.

job.params expects {
    "check_ids": ["uuid", ...],
    "update_fields": {...},           # CheckUpdate fields (interval, timeout, etc.)
    "new_agent_id": "uuid" | None,    # reassign to another agent
    "alert_id": "uuid" | "__clear__" | None,
}.
"""

from uuid import UUID

from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.maintenance_job_model import MaintenanceJob
from app.schemas.check_schema import CheckUpdate
from app.services.core.alert_core_service import AlertCoreService
from app.services.views.checks_view_service import ChecksViewService

logger = get_logger("luxswirl.maintenance.bulk_check_modify")


async def handle(db: AsyncSession, job: MaintenanceJob) -> None:
    raw_ids = job.params.get("check_ids") or []
    update_fields = job.params.get("update_fields") or {}
    new_agent_id = job.params.get("new_agent_id")
    alert_id = job.params.get("alert_id")

    if not raw_ids:
        logger.info("bulk_check_modify with empty check_ids — nothing to do")
        return

    check_ids = [UUID(c) if isinstance(c, str) else c for c in raw_ids]

    logger.info(
        "Bulk modifying checks",
        extra={"count": len(check_ids), "job_id": str(job.id)},
    )

    update_data = CheckUpdate(**update_fields)
    result = await ChecksViewService.bulk_modify(
        db=db,
        check_ids=check_ids,
        update_data=update_data,
        new_agent_id=new_agent_id,
    )

    if alert_id:
        if alert_id == "__clear__":
            await AlertCoreService.bulk_clear_from_checks(db, check_ids)
        else:
            await AlertCoreService.bulk_assign_to_checks(db, UUID(alert_id), check_ids)

    logger.info(
        "Bulk modify complete",
        extra={
            "success_count": result["success_count"],
            "failure_count": result["failure_count"],
            "job_id": str(job.id),
        },
    )
