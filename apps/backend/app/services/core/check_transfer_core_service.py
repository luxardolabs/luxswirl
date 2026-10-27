"""
Check transfer core service - an agent's checks exported, imported, or made in
bulk from URLs.

Import and bulk create can be thousands of inserts, so the request judges what
it can (the agent, the alerts, the size) and queues a maintenance job; the job
re-judges its owner when it runs. Each request is its own job: two imports for
one agent never collapse into the first.
"""

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    AgentNotFoundException,
    NotFoundException,
    ValidationException,
)
from app.models.enum_model import ImportMode, MaintenanceJobKind
from app.schemas.check_schema import BulkCheckCreateRequest
from app.schemas.import_export_schema import CheckExport, CheckExportDocument
from app.schemas.maintenance_job_schema import MaintenanceJobResponse
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.alert_core_service import AlertCoreService
from app.services.core.check_core_service import CheckCoreService
from app.services.core.maintenance_job_core_service import MaintenanceJobCoreService
from app.shared.logger import get_logger

logger = get_logger("luxswirl.services.check_transfer")

# The most checks one import or bulk create takes.
TRANSFER_MAX = 10_000


class CheckTransferCoreService:
    """Export, import and bulk-create an agent's checks."""

    @staticmethod
    async def export_document(db: AsyncSession, agent_id: UUID) -> CheckExportDocument:
        """The agent's checks in the form an import takes back.

        Raises:
            AgentNotFoundException: If there is no such agent
        """
        agent = await AgentCoreService.get_agent(db, agent_id)
        if not agent:
            raise AgentNotFoundException(str(agent_id))
        checks = await CheckCoreService.list_agent_checks(db, agent_id)
        return CheckExportDocument(
            agent_id=agent.id,
            agent_name=agent.agent_name,
            agent_hostname=agent.hostname,
            total_checks=len(checks),
            checks=[
                CheckExport(
                    name=check.display_name,
                    check_type=check.check_type,
                    target=check.target,
                    interval=check.interval_seconds or 60,
                    timeout=check.timeout_seconds or 5,
                    retry_attempts=check.retry_attempts or 2,
                    enabled=check.enabled,
                    description=check.description,
                    http_method=check.http_method,
                    expected_status=check.expected_status,
                    json_path=check.json_path,
                    expected_value=check.expected_value,
                    tags=check.tags,
                )
                for check in checks
            ],
        )

    @staticmethod
    async def enqueue_import(
        db: AsyncSession,
        owner_id: UUID,
        agent_id: UUID,
        mode: ImportMode,
        checks: list[dict[str, Any]],
    ) -> MaintenanceJobResponse:
        """Queue an import of `checks` onto the agent.

        Each entry is validated when the job runs, and one that is not a check
        is that entry's error. `replace` overwrites a check of the same name;
        `merge` skips it.

        Raises:
            AgentNotFoundException: If there is no such agent
            ValidationException: If there are no checks to import
        """
        if not await AgentCoreService.get_agent(db, agent_id):
            raise AgentNotFoundException(str(agent_id))
        if not checks:
            raise ValidationException("No checks to import")
        if len(checks) > TRANSFER_MAX:
            raise ValidationException(
                f"An import takes at most {TRANSFER_MAX:,} checks; this has {len(checks):,}"
            )
        job = await MaintenanceJobCoreService.enqueue(
            db,
            kind=MaintenanceJobKind.BULK_CHECK_IMPORT,
            target_id=None,
            params={"agent_id": str(agent_id), "mode": mode.value, "checks": checks},
            owner_id=owner_id,
        )
        logger.info(
            "Enqueued bulk_check_import maintenance job",
            extra={
                "agent_id": str(agent_id),
                "check_count": len(checks),
                "mode": mode.value,
                "job_id": str(job.id),
            },
        )
        return job

    @staticmethod
    async def enqueue_bulk_create(
        db: AsyncSession,
        owner_id: UUID,
        agent_id: UUID,
        requests: list[BulkCheckCreateRequest],
        alert_ids: list[UUID],
    ) -> MaintenanceJobResponse:
        """Queue one check per URL on the agent, each given the alerts.

        Raises:
            AgentNotFoundException: If there is no such agent
            ValidationException: If there are no URLs, the agent takes no
                checks, or an alert does not exist
        """
        if not requests:
            raise ValidationException("No URLs to create checks from")
        if len(requests) > TRANSFER_MAX:
            raise ValidationException(
                f"A bulk create takes at most {TRANSFER_MAX:,} URLs; this has {len(requests):,}"
            )
        await CheckCoreService.get_admitted_agent(db, agent_id)
        alerts = list(dict.fromkeys(alert_ids))
        for alert_id in alerts:
            try:
                await AlertCoreService.get_alert_by_id(db, alert_id)
            except NotFoundException:
                raise ValidationException(f"No alert has the id {alert_id}") from None
        job = await MaintenanceJobCoreService.enqueue(
            db,
            kind=MaintenanceJobKind.BULK_CHECK_CREATE,
            target_id=None,
            params={
                "agent_id": str(agent_id),
                "requests": [r.model_dump(mode="json") for r in requests],
                "alert_ids": [str(a) for a in alerts],
            },
            owner_id=owner_id,
        )
        logger.info(
            "Enqueued bulk_check_create maintenance job",
            extra={
                "agent_id": str(agent_id),
                "count": len(requests),
                "job_id": str(job.id),
            },
        )
        return job
