"""
Import/export core service - an agent's checks in the portable export format.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.check_crud import CheckCRUD
from app.schemas.import_export_schema import AgentChecksExport, CheckExport
from app.services.core.agent_core_service import AgentCoreService


class ImportExportCoreService:
    """Builds the check export a backup or a transfer to another agent reads."""

    @staticmethod
    async def export_agent_checks(
        db: AsyncSession, agent_id: UUID
    ) -> AgentChecksExport:
        """An agent's checks in the export format, for backup or transfer.

        Raises:
            AgentNotFoundException: If the agent does not exist
        """
        agent = await AgentCoreService.get_agent_by_id(db, agent_id)
        checks = await CheckCRUD.list_for_agent(db, agent_id)
        exported = [
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
        ]
        return AgentChecksExport(
            agent_id=agent_id,
            agent_hostname=agent.hostname,
            total_checks=len(exported),
            checks=exported,
        )
