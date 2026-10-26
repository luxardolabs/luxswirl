"""Handler for bulk_check_toggle maintenance jobs (enable/disable).

job.params expects {"action": "enable"|"disable", "check_ids": ["uuid", ...]}.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.maintenance_job_model import MaintenanceJob
from app.services.core.check_core_service import CheckCoreService
from app.services.core.user_core_service import UserCoreService
from app.shared.logger import get_logger

logger = get_logger("luxswirl.maintenance.bulk_check_toggle")


async def handle(db: AsyncSession, job: MaintenanceJob) -> None:
    action = job.params.get("action")
    raw_ids = job.params.get("check_ids") or []
    if action not in ("enable", "disable"):
        raise ValueError(
            f"bulk_check_toggle expects action enable|disable, got {action!r}"
        )
    if not raw_ids:
        logger.info("bulk_check_toggle with empty check_ids — nothing to do")
        return
    check_ids = [UUID(c) if isinstance(c, str) else c for c in raw_ids]
    logger.info(
        "Bulk %s checks",
        action,
        extra={"count": len(check_ids), "job_id": str(job.id)},
    )
    result = await CheckCoreService.bulk_set_enabled(
        db,
        check_ids,
        enabled=(action == "enable"),
        actor_is_admin=await UserCoreService.is_active_admin(db, job.owner_id),
    )
    logger.info(
        "Bulk %s complete",
        action,
        extra={
            "success_count": result["success_count"],
            "failure_count": result["failure_count"],
            "job_id": str(job.id),
        },
    )
    # The core reports a failed statement as failures rather than raising (its
    # savepoint keeps the session usable), so a job that changed nothing must
    # fail itself, or the worker finalises it as done. A partial result (some
    # checks already gone) completes with its counts logged above.
    if result["success_count"] == 0 and result["failure_count"]:
        raise RuntimeError("; ".join(result["errors"]))
