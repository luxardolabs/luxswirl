"""
Artifact router - HTTP endpoints for check artifact operations.

All business logic is delegated to ArtifactCoreService.
This router only handles HTTP concerns.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, status
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import UploadingAgentId
from app.db import get_db
from app.schemas.base_schema import ErrorResponse
from app.schemas.check_artifact_schema import (
    CheckArtifactCreate,
    CheckArtifactResponse,
)
from app.services.core.artifact_core_service import ArtifactCoreService

logger = get_logger("luxswirl.api.artifact")

router = APIRouter(tags=["Artifacts"])


@router.post(
    "/artifacts",
    response_model=CheckArtifactResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create artifact",
    description="An agent uploads an artifact (screenshot, trace, etc.) for one of its checks",
    responses={
        401: {"model": ErrorResponse, "description": "Not the agent's own key"},
        403: {
            "model": ErrorResponse,
            "description": "Agent not approved, or the check is not assigned to it",
        },
        404: {"model": ErrorResponse, "description": "Agent or check not found"},
    },
    name="create_artifact",
)
async def create_artifact(
    artifact: CheckArtifactCreate,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    agent_id: UploadingAgentId,
) -> CheckArtifactResponse:
    """
    Create a new check artifact.

    Called by agents after executing synthetic checks to upload screenshots,
    traces, and other binary artifacts. Only the agent's own key is accepted
    (`UploadingAgentId`), and only for a check assigned to that agent.
    """
    return await ArtifactCoreService.create_artifact(db, agent_id, artifact)
