"""
Artifact router - HTTP endpoints for check artifact operations.

All business logic is delegated to ArtifactCoreService.
This router only handles HTTP concerns.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_api_token
from app.db import get_db
from app.schemas.base_schema import ErrorResponse
from app.schemas.check_artifact_schema import (
    CheckArtifactCreate,
    CheckArtifactListResponse,
    CheckArtifactResponse,
    CheckArtifactStatsResponse,
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


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.get(
    "/artifacts/{artifact_id}",
    response_model=CheckArtifactResponse,
    summary="Get artifact metadata",
    description="Get artifact metadata (without binary data)",
    responses={
        404: {"model": ErrorResponse, "description": "Artifact not found"},
    },
    name="get_artifact_metadata",
)
async def get_artifact_metadata(
    artifact_id: Annotated[UUID, Path(description="Artifact UUID")],
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _: Annotated[str, Depends(verify_api_token)],
) -> CheckArtifactResponse:
    """Get artifact metadata without binary data."""
    artifact = await ArtifactCoreService.get_artifact(db, artifact_id)
    if artifact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Artifact not found: {artifact_id}",
        )
    return artifact


@router.get(
    "/artifacts/{artifact_id}/download",
    response_class=Response,
    summary="Download artifact",
    description="Download artifact binary data",
    responses={
        200: {"description": "Artifact binary data"},
        404: {"model": ErrorResponse, "description": "Artifact not found"},
    },
    name="download_artifact",
)
async def download_artifact(
    artifact_id: Annotated[UUID, Path(description="Artifact UUID")],
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _: Annotated[str, Depends(verify_api_token)],
) -> Response:
    """Download artifact binary data."""
    artifact = await ArtifactCoreService.get_artifact_content(db, artifact_id)
    if artifact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Artifact not found: {artifact_id}",
        )
    return Response(
        content=artifact.data,
        media_type=artifact.content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{artifact.filename}"',
            "Content-Length": str(artifact.size_bytes),
        },
    )


@router.get(
    "/checks/{check_id}/artifacts",
    response_model=CheckArtifactListResponse,
    summary="List artifacts for check",
    description="List all artifacts for a specific check",
    responses={
        404: {"model": ErrorResponse, "description": "Check not found"},
    },
    name="list_check_artifacts",
)
async def list_check_artifacts(
    check_id: Annotated[UUID, Path(description="Check UUID")],
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _: Annotated[str, Depends(verify_api_token)],
    limit: Annotated[
        int, Query(ge=1, le=500, description="Maximum number of artifacts to return")
    ] = 100,
) -> CheckArtifactListResponse:
    """List artifacts for a check (without binary data)."""
    return await ArtifactCoreService.list_artifacts_by_check(db, check_id, limit=limit)


@router.get(
    "/checks/{check_id}/results/{check_result_id}/artifacts",
    response_model=CheckArtifactListResponse,
    summary="List artifacts for check result",
    description="List artifacts for a specific check execution",
    responses={
        404: {"model": ErrorResponse, "description": "Check not found"},
    },
    name="list_check_result_artifacts",
)
async def list_check_result_artifacts(
    check_id: Annotated[UUID, Path(description="Check UUID")],
    check_result_id: Annotated[UUID, Path(description="Check result UUID")],
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _: Annotated[str, Depends(verify_api_token)],
) -> CheckArtifactListResponse:
    """List artifacts for a specific check result."""
    return await ArtifactCoreService.list_artifacts_by_check_result(
        db, check_id, check_result_id
    )


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.delete(
    "/artifacts/{artifact_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete artifact",
    description="Delete a specific artifact",
    responses={
        404: {"model": ErrorResponse, "description": "Artifact not found"},
    },
    name="delete_artifact",
)
async def delete_artifact(
    artifact_id: Annotated[UUID, Path(description="Artifact UUID")],
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _: Annotated[str, Depends(verify_api_token)],
) -> Response:
    """Delete an artifact."""
    deleted = await ArtifactCoreService.delete_artifact(db, artifact_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Artifact not found: {artifact_id}",
        )

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/checks/{check_id}/artifacts/stats",
    response_model=CheckArtifactStatsResponse,
    summary="Get artifact statistics",
    description="Get statistics about artifacts for a check",
    responses={
        404: {"model": ErrorResponse, "description": "Check not found"},
    },
    name="get_artifact_stats",
)
async def get_artifact_stats(
    check_id: Annotated[UUID, Path(description="Check UUID")],
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _: Annotated[str, Depends(verify_api_token)],
) -> CheckArtifactStatsResponse:
    """Get artifact statistics for a check."""
    return await ArtifactCoreService.get_artifact_stats(db, check_id)
