"""Handler for bulk_check_import maintenance jobs.

job.params expects {"agent_id": str, "mode": "merge"|"replace", "checks": [...]}.

Import is an admin's: the job runs only while its owner is still an active
admin, and only onto an agent that still exists. It keeps what it did (counts
and first errors) on the job.
"""

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.background.maintenance_handlers._job_owner import require_owner_is_admin
from app.background.maintenance_worker import JobFailed
from app.core.exceptions import AgentNotFoundException, ValidationException
from app.models.enum_model import ImportMode
from app.models.maintenance_job_model import MaintenanceJob
from app.services.core.check_core_service import CheckCoreService
from app.services.core.maintenance_job_core_service import MaintenanceJobCoreService
from app.shared.logger import get_logger

logger = get_logger("luxswirl.maintenance.bulk_check_import")

# The most per-check errors a job keeps; the counts are always whole.
_ERRORS_KEPT = 50


async def handle(db: AsyncSession, job: MaintenanceJob) -> None:
    agent_id_raw = job.params.get("agent_id")
    checks_data = job.params.get("checks") or []
    mode = ImportMode(job.params.get("mode") or ImportMode.MERGE)

    if not agent_id_raw:
        raise ValueError("bulk_check_import job requires params.agent_id")
    if not checks_data:
        logger.info("bulk_check_import with empty checks — nothing to do")
        return

    await require_owner_is_admin(db, job)

    agent_id = UUID(agent_id_raw) if isinstance(agent_id_raw, str) else agent_id_raw

    logger.info(
        "Importing checks",
        extra={
            "agent_id": str(agent_id),
            "count": len(checks_data),
            "mode": mode.value,
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
    try:
        result = await CheckCoreService.bulk_import_checks(
            db,
            agent_id,
            checks,
            overwrite=mode == ImportMode.REPLACE,
            actor_is_admin=True,
        )
    except AgentNotFoundException:
        raise JobFailed("The agent no longer exists; nothing was imported") from None
    except ValidationException as e:
        raise JobFailed(f"{e.message}; nothing was imported") from None

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
    progress = _outcome(result)
    # An import that changed no check and failed some fails, its outcome kept.
    if result["created"] + result["updated"] == 0 and result["errors"]:
        raise JobFailed("; ".join(progress["errors"]), progress)
    await MaintenanceJobCoreService.update_progress(db, job.id, progress)


def _outcome(result: dict[str, Any]) -> dict[str, Any]:
    """The import's counts and first errors, as anyone who polls the job may read them."""
    errors = result["errors"]
    created, updated, skipped = result["created"], result["updated"], result["skipped"]
    message = (
        f"Imported {created + updated} check(s): {created} created, {updated} updated"
    )
    if skipped:
        message += f", {skipped} skipped"
    if errors:
        message += f"; {len(errors)} failed"
    return {
        "message": message,
        "total": created + updated + skipped + len(errors),
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "failure_count": len(errors),
        "errors": [
            MaintenanceJobCoreService.readable_error(f"{e['check']}: {e['error']}")
            for e in errors[:_ERRORS_KEPT]
        ],
    }
