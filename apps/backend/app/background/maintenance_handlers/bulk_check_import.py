"""Handler for bulk_check_import maintenance jobs.

job.params expects {"agent_id": str, "mode": "merge"|"replace", "checks": [...]}.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.maintenance_job_model import MaintenanceJob
from app.services.core.check_core_service import CheckCoreService
from app.services.core.maintenance_job_core_service import MaintenanceJobCoreService
from app.services.core.user_core_service import UserCoreService
from app.shared.logger import get_logger

logger = get_logger("luxswirl.maintenance.bulk_check_import")


async def handle(db: AsyncSession, job: MaintenanceJob) -> None:
    agent_id_raw = job.params.get("agent_id")
    checks_data = job.params.get("checks") or []
    mode = job.params.get("mode") or "merge"

    if not agent_id_raw:
        raise ValueError("bulk_check_import job requires params.agent_id")
    if not checks_data:
        logger.info("bulk_check_import with empty checks — nothing to do")
        return

    agent_id = UUID(agent_id_raw) if isinstance(agent_id_raw, str) else agent_id_raw

    logger.info(
        "Importing checks",
        extra={
            "agent_id": str(agent_id),
            "count": len(checks_data),
            "mode": mode,
            "job_id": str(job.id),
        },
    )

    await MaintenanceJobCoreService.update_progress(
        db,
        job.id,
        {
            "message": f"Importing {len(checks_data)} check(s)",
            "total": len(checks_data),
        },
    )

    # The upload was only structurally checked when it was enqueued; the import
    # validates each entry, and one that is not a check is an error, not an abort.
    checks = [raw if isinstance(raw, dict) else {"name": None} for raw in checks_data]
    # The upload route is admin-only (AdminUserWeb), so the job acts as admin.
    result = await CheckCoreService.bulk_import_checks(
        db,
        agent_id,
        checks,
        overwrite=(mode == "replace"),
        actor_is_admin=await UserCoreService.is_active_admin(db, job.owner_id),
    )

    logger.info(
        "Bulk check import complete",
        extra={
            "agent_id": str(agent_id),
            "created_count": result["created"],
            "updated": result["updated"],
            "skipped": result["skipped"],
            "errors_count": len(result["errors"]),
            "job_id": str(job.id),
        },
    )
