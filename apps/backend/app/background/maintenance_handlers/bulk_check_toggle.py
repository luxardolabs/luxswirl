"""Handler for bulk_check_toggle maintenance jobs (enable/disable).

job.params expects {"action": "enable"|"disable", "check_ids": ["uuid", ...]}.
"""

from uuid import UUID

from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.maintenance_job_model import MaintenanceJob
from app.services.views.checks_view_service import ChecksViewService

logger = get_logger("luxswirl.maintenance.bulk_check_toggle")


async def handle(db: AsyncSession, job: MaintenanceJob) -> None:
    action = job.params.get("action")
    raw_ids = job.params.get("check_ids") or []
    if action not in ("enable", "disable"):
        raise ValueError(f"bulk_check_toggle expects action enable|disable, got {action!r}")
    if not raw_ids:
        logger.info("bulk_check_toggle with empty check_ids — nothing to do")
        return
    check_ids = [UUID(c) if isinstance(c, str) else c for c in raw_ids]
    logger.info(
        "Bulk %s checks",
        action,
        extra={"count": len(check_ids), "job_id": str(job.id)},
    )
    result = await ChecksViewService.bulk_action(db, check_ids, action)
    logger.info(
        "Bulk %s complete",
        action,
        extra={
            "success_count": result["success_count"],
            "failure_count": result["failure_count"],
            "job_id": str(job.id),
        },
    )
