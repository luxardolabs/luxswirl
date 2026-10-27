"""
Import/Export service - handles bulk check import/export logic.

This service provides web-specific logic for transforming checks to/from export format
and orchestrating the import process with proper error handling.
"""

import json
from io import BytesIO
from typing import Any
from uuid import UUID

from fastapi import Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.content_disposition import content_disposition
from app.core.datetime_utils import utc_now
from app.core.exceptions import AgentNotFoundException, ValidationException
from app.models.enum_model import ImportMode
from app.schemas.auth_schema import AuthedUser
from app.services.core.agent_core_service import AgentCoreService
from app.services.core.check_core_service import CheckCoreService
from app.services.core.check_transfer_core_service import CheckTransferCoreService
from app.services.views._job_status import build_job_status_context
from app.shared.logger import get_logger
from app.web.templates import templates

logger = get_logger("luxswirl.web.services.import_export")


class ImportExportViewService:
    """Service for check import/export operations."""

    @staticmethod
    async def build_panel_context(
        db: AsyncSession, request: Request, current_user: AuthedUser, agent_id: UUID
    ) -> dict[str, Any]:
        """Import/export panel context for an agent.

        Raises AgentNotFoundException if the agent is gone.
        """
        agent = await AgentCoreService.get_agent(db, agent_id)
        if not agent:
            raise AgentNotFoundException(str(agent_id))
        counts = await CheckCoreService.check_counts_by_agent(db, [agent_id])
        return {
            "current_user": current_user,
            "agent": agent,
            "agent_id": agent_id,
            "check_count": counts[agent_id].total,
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
        document = await CheckTransferCoreService.export_document(db, agent_id)
        json_bytes = BytesIO(
            json.dumps(document.model_dump(mode="json"), indent=2).encode()
        )
        # Filename stamp: machine output, so it stays here — but UTC and sortable.
        # `datetime.now()` here was naive server-local time.
        filename = f"{agent_id}-checks-{utc_now().strftime('%Y%m%d_%H%M%S')}.json"
        return StreamingResponse(
            json_bytes,
            media_type="application/json",
            headers={
                "Content-Disposition": content_disposition("attachment", filename)
            },
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
        mode: ImportMode,
    ) -> Response:
        """Parse + structurally validate the upload, enqueue a bulk_check_import
        job, and return the polling partial.

        File parsing stays in the request (kilobytes); the actual import — which
        for mode=replace cascades through check_results — runs in the worker.
        No explicit commit: get_db() commits on clean return
        before the response is sent, so the job is visible when the client polls.
        """
        try:
            try:
                data = json.loads(content)
            except json.JSONDecodeError as e:
                return ImportExportViewService._import_result_response(
                    request, current_user, f"Invalid JSON file: {e}", 400
                )

            checks_data = data.get("checks") if isinstance(data, dict) else None
            if not checks_data or not isinstance(checks_data, list):
                return ImportExportViewService._import_result_response(
                    request, current_user, "No checks found in file", 400
                )

            # An entry that is not an object is that entry's error when the job runs.
            entries = [
                c if isinstance(c, dict) else {"name": None} for c in checks_data
            ]
            job = await CheckTransferCoreService.enqueue_import(
                db, current_user.id, agent_id, mode, entries
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
        except ValidationException as e:
            return ImportExportViewService._import_result_response(
                request, current_user, e.message, 400
            )
        # swallowed-exceptions: the import surface renders the failure back to
        # the operator with the reason, which is the handling.
        except Exception as e:
            logger.exception("Error during import")
            return ImportExportViewService._import_result_response(
                request, current_user, str(e), 500
            )
