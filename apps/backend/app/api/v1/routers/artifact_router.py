"""
Artifact router - HTTP endpoints for check artifact operations.

All business logic is delegated to ArtifactCoreService.
This router only handles HTTP concerns.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, status
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_api_token
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
    description="Upload a check artifact (screenshot, trace, etc.) - typically called by agents",
    responses={
        404: {"model": ErrorResponse, "description": "Check not found"},
    },
    name="create_artifact",
)
async def create_artifact(
    artifact: CheckArtifactCreate,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _: Annotated[str, Depends(verify_api_token)],
) -> CheckArtifactResponse:
    """
    Create a new check artifact.

    This endpoint is typically called by agents after executing synthetic checks
    to upload screenshots, traces, and other binary artifacts.
    """
    return await ArtifactCoreService.create_artifact(db, artifact)
