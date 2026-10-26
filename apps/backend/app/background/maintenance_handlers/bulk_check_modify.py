"""Handler for bulk_check_modify maintenance jobs.

job.params expects {
    "check_ids": ["uuid", ...],
    "update_fields": {...},           # CheckUpdate fields (interval, timeout, etc.)
    "new_agent_id": "uuid" | None,    # reassign to another agent
    "alert_id": "uuid" | "__clear__" | None,
}.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.maintenance_job_model import MaintenanceJob
from app.schemas.check_schema import CheckUpdate
from app.services.core.alert_core_service import AlertCoreService
from app.services.core.check_core_service import CheckCoreService
from app.services.core.user_core_service import UserCoreService
from app.shared.logger import get_logger

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
    result = await CheckCoreService.bulk_modify(
        db=db,
        check_ids=check_ids,
        update_data=update_data,
        new_agent_id=UUID(new_agent_id) if new_agent_id else None,
        actor_is_admin=await UserCoreService.is_active_admin(db, job.owner_id),
    )

    # Alerts go on the checks the modify changed: never on one it refused.
    changed = result["changed_ids"]
    if alert_id and changed:
        if alert_id == "__clear__":
            await AlertCoreService.bulk_clear_from_checks(db, changed)
        else:
            await AlertCoreService.bulk_assign_to_checks(db, UUID(alert_id), changed)

    logger.info(
        "Bulk modify complete",
        extra={
            "success_count": result["success_count"],
            "failure_count": result["failure_count"],
            "job_id": str(job.id),
        },
    )
