"""Handler for bulk_check_delete maintenance jobs.

job.params expects {"check_ids": ["uuid", ...]}.

Delegates to CheckCoreService.bulk_delete so the cascade through
check_results AND the business invariant — bumping each affected agent's
checks_updated_at to trigger a config reload — both run. Calling CheckCRUD
directly here previously dropped the timestamp bump, leaving deleted checks
still executing on the agent until an unrelated config change.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.maintenance_job_model import MaintenanceJob
from app.services.core.check_core_service import CheckCoreService
from app.services.core.user_core_service import UserCoreService
from app.shared.logger import get_logger

logger = get_logger("luxswirl.maintenance.bulk_check_delete")


async def handle(db: AsyncSession, job: MaintenanceJob) -> None:
    raw_ids = job.params.get("check_ids") or []
    if not raw_ids:
        logger.info("bulk_check_delete with empty check_ids — nothing to do")
        return
    check_ids = [UUID(c) if isinstance(c, str) else c for c in raw_ids]
    logger.info(
        "Bulk deleting checks (cascade)",
        extra={"count": len(check_ids), "job_id": str(job.id)},
    )
    result = await CheckCoreService.bulk_delete(
        db,
        check_ids,
        actor_is_admin=await UserCoreService.is_active_admin(db, job.owner_id),
    )
    logger.info(
        "Bulk deleted checks",
        extra={"result": result, "job_id": str(job.id)},
    )
    # The core reports a failed statement as failures rather than raising (its
    # savepoint keeps the session usable), so a job that changed nothing must
    # fail itself, or the worker finalises it as done. A partial result (some
    # checks already gone) completes with its counts logged above.
    if result["success_count"] == 0 and result["failure_count"]:
        raise RuntimeError("; ".join(result["errors"]))
