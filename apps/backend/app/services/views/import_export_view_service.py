"""
Import/Export service - handles bulk check import/export logic.

This service provides web-specific logic for transforming checks to/from export format
and orchestrating the import process with proper error handling.
"""

import json
from collections.abc import Sequence
from io import BytesIO
from typing import Any
from uuid import UUID

from fastapi import Request
from fastapi.responses import Response, StreamingResponse
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_deps import AuthedUser
from app.core.datetime_utils import utc_now
from app.core.exceptions import AgentNotFoundException
from app.models.agent_model import Agent
from app.models.check_model import Check
from app.models.enum_model import CheckType, MaintenanceJobKind
from app.schemas.check_schema import CheckCreate, CheckUpdate
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.check_core_service import CheckCoreService
from app.services.core.maintenance_job_core_service import MaintenanceJobCoreService
from app.services.views._job_status import build_job_status_context
from app.web.templates_config import templates

logger = get_logger("luxswirl.web.services.import_export")


class ImportResult:
    """Result of an import operation."""

    def __init__(self):
        self.created = 0
        self.updated = 0
        self.skipped = 0
        self.errors = []

    @property
    def total_processed(self) -> int:
        """Total checks processed (created + updated + skipped + errors)."""
        return int(self.created + self.updated + self.skipped + len(self.errors))

    @property
    def has_errors(self) -> bool:
        """Whether any errors occurred."""
        return len(self.errors) > 0


class ImportExportViewService:
    """Service for check import/export operations."""

    @staticmethod
    def export_checks_to_dict(checks: Sequence[Check], agent: Agent) -> dict:
        """
        Transform checks to export format.

        Args:
            checks: List of Check models
            agent: Agent model

        Returns:
            Dict with export data (agent info + checks array)
        """
        export_checks = []
        for check in checks:
            export_checks.append(
                {
                    "name": check.display_name,
                    "check_type": check.check_type,
                    "target": check.target,
                    "interval": check.interval_seconds or 60,
                    "timeout": check.timeout_seconds or 5,
                    "retry_attempts": check.retry_attempts or 2,
                    "enabled": check.enabled,
                    "description": check.description,
                    "http_method": check.http_method,
                    "expected_status": check.expected_status,
                    "json_path": check.json_path,
                    "expected_value": check.expected_value,
                    "tags": check.tags,
                }
            )

        return {
            "agent_id": str(agent.id),
            "agent_name": agent.agent_name,
            "agent_hostname": agent.hostname,
            "total_checks": len(export_checks),
            "checks": export_checks,
        }

    @staticmethod
    async def import_checks_from_data(
        db: AsyncSession,
        agent_id: UUID,
        checks_data: list[dict],
        mode: str,
    ) -> ImportResult:
        """
        Import checks from parsed JSON data.

        Args:
            db: Database session
            agent_id: Agent UUID to import checks for
            checks_data: List of check dictionaries from JSON
            mode: "merge" (skip existing) or "replace" (update existing)

        Returns:
            ImportResult with counts and errors
        """
        result = ImportResult()

        # Get existing checks
        existing_checks = await CheckCoreService.list_checks_for_agent(db, agent_id)
        existing_map = {c.display_name: c for c in existing_checks}

        # Process each check
        for check_data in checks_data:
            try:
                check_name = check_data.get("name")
                if not check_name:
                    result.errors.append(
                        {"check": "unknown", "error": "Missing check name"}
                    )
                    continue

                # Check if exists
                if check_name in existing_map:
                    if mode == "replace":
                        # Update existing check
                        existing_check = existing_map[check_name]
                        await CheckCoreService.update_check(
                            db,
                            existing_check.id,
                            CheckUpdate(
                                check_type=check_data.get("check_type"),
                                target=check_data.get("target"),
                                interval_seconds=check_data.get("interval", 60),
                                timeout_seconds=check_data.get("timeout", 5),
                                retry_attempts=check_data.get("retry_attempts", 2),
                                enabled=check_data.get("enabled", True),
                                description=check_data.get("description"),
                                http_method=check_data.get("http_method"),
                                expected_status=check_data.get("expected_status"),
                                json_path=check_data.get("json_path"),
                                expected_value=check_data.get("expected_value"),
                                tags=check_data.get("tags"),
                            ),
                            # Import is reachable only via the AdminUserWeb-gated route.
                            actor_is_admin=True,
                        )
                        result.updated += 1
                    else:  # merge mode - skip existing
                        result.skipped += 1
                else:
                    # Create new check
                    check_type = check_data.get("check_type")
                    target = check_data.get("target")
                    if not check_type or not target:
                        result.errors.append(
                            {
                                "check": check_name,
                                "error": "Missing check_type or target",
                            }
                        )
                        continue
                    await CheckCoreService.create_check(
                        db,
                        agent_id,
                        CheckCreate(
                            display_name=check_name,
                            check_type=CheckType(check_type),
                            target=target,
                            interval_seconds=check_data.get("interval", 60),
                            timeout_seconds=check_data.get("timeout", 5),
                            retries=check_data.get("retries", 2),
                            enabled=check_data.get("enabled", True),
                            description=check_data.get("description"),
                            http_method=check_data.get("http_method"),
                            expected_status=check_data.get("expected_status"),
                            json_path=check_data.get("json_path"),
                            expected_value=check_data.get("expected_value"),
                            tags=check_data.get("tags"),
                        ),
                        # Import is reachable only via the AdminUserWeb-gated route.
                        actor_is_admin=True,
                    )
                    result.created += 1

            except Exception as e:
                result.errors.append(
                    {"check": check_data.get("name", "unknown"), "error": str(e)}
                )
                logger.error(
                    "Error importing check",
                    extra={"check_name": check_data.get("name")},
                    exc_info=True,
                )

        logger.info(
            "Import complete for agent",
            extra={
                "agent_id": str(agent_id),
                "created_count": result.created,
                "updated_count": result.updated,
                "skipped_count": result.skipped,
                "error_count": len(result.errors),
            },
        )

        return result

    @staticmethod
    async def build_panel_context(
        db: AsyncSession, request: Request, current_user: AuthedUser, agent_id: UUID
    ) -> dict[str, Any]:
        """Import/export panel context for an agent.

        Raises AgentNotFoundException if the agent is gone.
        """
        agent = await AgentCoreService.get_agent_by_id(db, agent_id)
        if not agent:
            raise AgentNotFoundException(str(agent_id))
        checks = await CheckCoreService.list_checks_for_agent(db, agent_id)
        return {
            "current_user": current_user,
            "agent": agent,
            "agent_id": agent_id,
            "check_count": len(checks),
            # .path, not str(): url_for returns an absolute url from the
            # request's scheme/host, which behind TLS-terminating nginx can emit
            # http:// into an https:// page.
            "export_url": request.url_for("web_export_checks", agent_id=agent_id).path,
            "import_url": request.url_for("web_import_checks", agent_id=agent_id).path,
        }

    @staticmethod
    async def build_export_download(
        db: AsyncSession, agent_id: UUID
    ) -> StreamingResponse:
        """Export an agent's checks as a timestamped JSON download.

        Raises AgentNotFoundException if the agent is gone.
        """
        agent = await AgentCoreService.get_agent_by_id(db, agent_id)
        if not agent:
            raise AgentNotFoundException(str(agent_id))
        checks = await CheckCoreService.list_checks_for_agent(db, agent_id)
        export_data = ImportExportViewService.export_checks_to_dict(checks, agent)
        json_bytes = BytesIO(json.dumps(export_data, indent=2).encode())
        # Filename stamp: machine output, so it stays here — but UTC and sortable.
        # `datetime.now()` here was naive server-local time.
        filename = f"{agent_id}-checks-{utc_now().strftime('%Y%m%d_%H%M%S')}.json"
        return StreamingResponse(
            json_bytes,
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    @staticmethod
    def _import_result_response(
        request: Request, current_user: AuthedUser, error: str, status_code: int
    ) -> Response:
        """Render the import-result partial for an error/validation failure."""
        return templates.TemplateResponse(
            request,
            "partials/import_result.html",
            {"current_user": current_user, "success": False, "error": error},
            status_code=status_code,
        )

    @staticmethod
    async def handle_import(
        db: AsyncSession,
        request: Request,
        current_user: AuthedUser,
        agent_id: UUID,
        content: bytes,
        mode: str,
    ) -> Response:
        """Parse + structurally validate the upload, enqueue a bulk_check_import
        job, and return the polling partial.

        File parsing stays in the request (kilobytes); the actual import — which
        for mode=replace cascades through check_results — runs in the worker.
        See LUXSWIRL-105. No explicit commit: get_db() commits on clean return
        before the response is sent, so the job is visible when the client polls.
        """
        try:
            agent = await AgentCoreService.get_agent_by_id(db, agent_id)
            if not agent:
                raise AgentNotFoundException(str(agent_id))

            try:
                data = json.loads(content)
            except json.JSONDecodeError as e:
                return ImportExportViewService._import_result_response(
                    request, current_user, f"Invalid JSON file: {e}", 400
                )

            checks_data = data.get("checks", [])
            if not checks_data:
                return ImportExportViewService._import_result_response(
                    request, current_user, "No checks found in file", 400
                )

            job = await MaintenanceJobCoreService.enqueue(
                db,
                kind=MaintenanceJobKind.BULK_CHECK_IMPORT,
                target_id=agent_id,
                params={
                    "agent_id": str(agent_id),
                    "mode": mode,
                    "checks": checks_data,
                },
                owner_id=current_user.id,
            )
            logger.info(
                "Enqueued bulk_check_import maintenance job",
                extra={
                    "agent_id": str(agent_id),
                    "check_count": len(checks_data),
                    "mode": mode,
                    "job_id": str(job.id),
                },
            )
            return templates.TemplateResponse(
                request,
                "partials/maintenance/job_status.html",
                build_job_status_context(request, current_user, job),
            )
        except AgentNotFoundException:
            return ImportExportViewService._import_result_response(
                request, current_user, f"Agent '{agent_id}' not found", 404
            )
        except Exception as e:
            logger.error("Error during import", exc_info=True)
            return ImportExportViewService._import_result_response(
                request, current_user, str(e), 500
            )
