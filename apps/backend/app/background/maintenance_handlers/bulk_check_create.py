"""Handler for bulk_check_create maintenance jobs.

job.params expects {
    "agent_id": "uuid",
    "requests": [BulkCheckCreateRequest dict, ...],
    "alert_ids": ["uuid", ...] | [],
}.

The job runs for its owner only while they may still edit, and keeps what it
made (counts and first errors) on the job.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.background.maintenance_handlers._bulk_outcome import finish
from app.background.maintenance_handlers._job_owner import require_owner_may_edit
from app.core.credential_mask import mask_credentials
from app.core.exceptions import NotFoundException
from app.models.enum_model import BulkCheckOutcome
from app.models.maintenance_job_model import MaintenanceJob
from app.schemas.check_schema import BulkCheckCreateRequest
from app.services.core.alert_core_service import AlertCoreService
from app.services.core.check_core_service import CheckCoreService
from app.shared.logger import get_logger

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

    await require_owner_may_edit(db, job)

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

    result = await CheckCoreService.bulk_create_checks(db, agent_id, requests)

    # An alert deleted since the job was queued is reported, not assigned.
    alert_errors: list[str] = []
    check_ids = [r.check_id for r in result.results if r.check_id]
    if check_ids:
        for alert_id_str in alert_ids:
            if not alert_id_str:
                continue
            try:
                await AlertCoreService.get_alert_by_id(db, UUID(alert_id_str))
            except NotFoundException:
                alert_errors.append(
                    f"Alert {alert_id_str} no longer exists; it was not assigned"
                )
                continue
            await AlertCoreService.bulk_assign_to_checks(
                db, UUID(alert_id_str), check_ids
            )

    logger.info(
        "Bulk create complete",
        extra={
            "succeeded": result.succeeded,
            "failed": result.failed,
            "job_id": str(job.id),
        },
    )
    await finish(
        db,
        job,
        "Created",
        {
            "success_count": result.succeeded,
            "failure_count": result.failed,
            "errors": [
                f"{mask_credentials(r.url)}: {r.error or 'not created'}"
                for r in result.results
                if r.status == BulkCheckOutcome.FAILED
            ]
            + alert_errors,
        },
        fail_when_nothing_done=True,
    )
