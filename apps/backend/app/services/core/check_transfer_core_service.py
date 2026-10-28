"""
Check transfer core service - an agent's checks exported, imported, or made in
bulk from URLs.

Import and bulk create can be thousands of inserts, so the request judges what
it can (the agent, the alerts, the size) and queues a maintenance job; the job
re-judges its owner when it runs. A job is keyed by what it asks for: two
different imports for one agent are two jobs, and the same one sent twice
while the first is queued is one.
"""

import json
from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    AgentNotFoundException,
    NotFoundException,
    ValidationException,
)
from app.models.enum_model import ImportMode, MaintenanceJobKind
from app.schemas.check_schema import BulkCheckCreateRequest, CheckInDB
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

# Names a job by what it asks for, so the same request sent twice while the
# first is queued (a double click, a client's retry) is one job.
_REQUEST_KEYS = UUID("6c0c3b0e-3f69-4f0e-9a39-6f1f7b8f2a51")


def _request_key(
    kind: MaintenanceJobKind, owner_id: UUID, params: dict[str, Any]
) -> UUID:
    body = json.dumps(
        {"kind": kind.value, "owner": str(owner_id), "params": params}, sort_keys=True
    )
    return uuid5(_REQUEST_KEYS, body)


def _or(value: int | None, default: int) -> int:
    """`value`, or `default` when it is unset (a 0 is kept)."""
    return default if value is None else value


def _entry(check: CheckInDB, names: dict[UUID, str]) -> CheckExport:
    """One check as the export carries it; `names` are its agent's checks by id.

    A parent on the same agent is named, so an import onto another agent
    points at that agent's check of the name; a parent elsewhere is its id.
    """
    parent = check.depends_on_check_id
    parent_name = names.get(parent) if parent is not None else None
    return CheckExport(
        name=check.display_name,
        check_type=check.check_type,
        target=check.target,
        interval=_or(check.interval_seconds, 60),
        timeout=_or(check.timeout_seconds, 5),
        retry_attempts=_or(check.retry_attempts, 2),
        enabled=check.enabled,
        description=check.description,
        http_method=check.http_method,
        expected_status=check.expected_status,
        json_path=check.json_path,
        expected_value=check.expected_value,
        tags=check.tags,
        retry_interval_seconds=check.retry_interval_seconds,
        resend_notification_after=check.resend_notification_after,
        verify_ssl=check.verify_ssl,
        record_type=check.record_type,
        nameserver=check.nameserver,
        port=check.port,
        expect_value=check.expect_value,
        connection_string=check.connection_string,
        query=check.query,
        script_code=check.script_code,
        depends_on=parent_name,
        depends_on_check_id=parent if parent_name is None else None,
    )


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
        names = {check.id: check.display_name for check in checks}
        return CheckExportDocument(
            agent_id=agent.id,
            agent_name=agent.agent_name,
            agent_hostname=agent.hostname,
            total_checks=len(checks),
            checks=[_entry(check, names) for check in checks],
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
        if not checks:
            raise ValidationException("No checks to import")
        if len(checks) > TRANSFER_MAX:
            raise ValidationException(
                f"An import takes at most {TRANSFER_MAX:,} checks; this has {len(checks):,}"
            )
        await CheckCoreService.get_admitted_agent(db, agent_id)
        params = {"agent_id": str(agent_id), "mode": mode.value, "checks": checks}
        job = await MaintenanceJobCoreService.enqueue(
            db,
            kind=MaintenanceJobKind.BULK_CHECK_IMPORT,
            target_id=_request_key(
                MaintenanceJobKind.BULK_CHECK_IMPORT, owner_id, params
            ),
            params=params,
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
    async def enqueue_import_checks(
        db: AsyncSession,
        owner_id: UUID,
        agent_id: UUID,
        mode: ImportMode,
        checks: list[CheckExport],
    ) -> MaintenanceJobResponse:
        """Queue an import of checks already held to the export's form."""
        return await CheckTransferCoreService.enqueue_import(
            db,
            owner_id,
            agent_id,
            mode,
            [check.model_dump(mode="json") for check in checks],
        )

    @staticmethod
    async def enqueue_bulk_create(
        db: AsyncSession,
        owner_id: UUID,
        agent_id: UUID,
        requests: Sequence[BulkCheckCreateRequest],
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
        params = {
            "agent_id": str(agent_id),
            "requests": [r.model_dump(mode="json") for r in requests],
            "alert_ids": [str(a) for a in alerts],
        }
        job = await MaintenanceJobCoreService.enqueue(
            db,
            kind=MaintenanceJobKind.BULK_CHECK_CREATE,
            target_id=_request_key(
                MaintenanceJobKind.BULK_CHECK_CREATE, owner_id, params
            ),
            params=params,
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
