"""Handler for bulk_check_delete maintenance jobs.

job.params expects {"check_ids": ["uuid", ...]}.

Delegates to CheckCoreService.bulk_action(action="delete") (via ChecksViewService,
matching the sibling toggle/modify handlers) so the cascade through
check_results AND the business invariant — bumping each affected agent's
checks_updated_at to trigger a config reload — both run. Calling CheckCRUD
directly here previously dropped the timestamp bump, leaving deleted checks
still executing on the agent until an unrelated config change.
"""

from uuid import UUID

from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.maintenance_job_model import MaintenanceJob
from app.services.views.checks_view_service import ChecksViewService

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
    result = await ChecksViewService.bulk_action(db, check_ids, "delete")
    logger.info(
        "Bulk deleted checks",
        extra={"result": result, "job_id": str(job.id)},
    )
