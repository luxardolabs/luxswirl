"""
Import/Export router - bulk import/export of checks.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, status
from shared.config import get_config
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_api_token
from app.db import get_db
from app.schemas.import_export_schema import (
    AgentChecksExport,
    AgentConfigExport,
    BulkImportRequest,
    BulkImportResponse,
    CheckExport,
)
from app.services.core.check_core_service import CheckCoreService
from app.services.core.import_export_core_service import ImportExportCoreService

logger = get_logger("luxswirl.api.import_export")

router = APIRouter(prefix="/import-export", tags=["Import/Export"])


@router.get(
    "/export/{agent_id}",
    response_model=AgentChecksExport,
)
async def export_checks(
    agent_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
) -> AgentChecksExport:
    """
    Export all checks for an agent.

    Args:
        agent_id: Agent identifier
        db: Database session

    Returns:
        The agent and its checks in the export format
    """
    return await ImportExportCoreService.export_agent_checks(db, agent_id)


@router.post("/import", response_model=BulkImportResponse)
async def import_checks(
    request: Annotated[BulkImportRequest, Body()],
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
) -> BulkImportResponse:
    """
    Bulk import checks for an agent.

    Args:
        request: Import request with agent_id and checks
        db: Database session

    Returns:
        Import summary with counts
    """
    logger.info(
        "Importing checks for agent",
        extra={"check_count": len(request.checks), "agent_id": str(request.agent_id)},
    )

    try:
        # All DTO construction + the create/update loop live in the core service
        # (LUXSWIRL-168) — the router just hands off and shapes the response.
        # Every JSON-API bearer token is an admin credential (SECURITY.md).
        result = await CheckCoreService.bulk_import_checks(
            db, request.agent_id, request.checks, request.overwrite, actor_is_admin=True
        )
        # Prefixed keys: `created` is a reserved LogRecord attribute, so
        # splatting the result dict into `extra` raised and turned every
        # successful import into a 500.
        logger.info(
            "Import complete",
            extra={
                "agent_id": str(request.agent_id),
                "checks_created": result["created"],
                "checks_updated": result["updated"],
                "checks_skipped": result["skipped"],
                "check_error_count": len(result["errors"]),
            },
        )
        return BulkImportResponse(total=len(request.checks), **result)
    except Exception as e:
        logger.exception("Error during bulk import")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        ) from e


@router.get(
    "/export-agent-config",
    response_model=AgentConfigExport,
)
async def export_agent_config(
    _: Annotated[str, Depends(verify_api_token)],
) -> AgentConfigExport:
    """
    Export the default agent configuration with all checks.

    This exports the checks from the DEFAULT_AGENT_CONFIG which can be used
    to seed the database with all checks.

    Returns:
        JSON with agent_id and checks list
    """

    config = get_config("agent")
    checks = config.get("checks", [])
    agent_id = config.get("agent_id", "docker-agent")

    export_checks = [
        CheckExport.model_validate(
            {
                "name": check.get("name"),
                "check_type": check.get("check_type"),
                "target": check.get("target"),
                "interval": check.get("interval", 60),
                "timeout": check.get("timeout", 5),
                "retry_attempts": check.get("retry_attempts", 2),
                "enabled": check.get("enabled", True),
                "description": check.get("description"),
                "http_method": check.get("http_method"),
                "expected_status": check.get("expected_status"),
                "json_path": check.get("json_path"),
                "expected_value": check.get("expected_value"),
                "tags": check.get("tags"),
            }
        )
        for check in checks
    ]
    return AgentConfigExport(
        agent_id=str(agent_id), total_checks=len(export_checks), checks=export_checks
    )
