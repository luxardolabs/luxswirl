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
from app.schemas.base import ErrorResponse
from app.schemas.check_artifact_schema import (
    CheckArtifactCreate,
    CheckArtifactListResponse,
    CheckArtifactResponse,
)
from app.services.core.artifact_core_service import ArtifactCoreService
from app.services.core.check_core_service import CheckCoreService

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
)
async def create_artifact(
    artifact: CheckArtifactCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
):
    """
    Create a new check artifact.

    This endpoint is typically called by agents after executing synthetic checks
    to upload screenshots, traces, and other binary artifacts.
    """
    try:
        # Verify check exists
        check = await CheckCoreService.get_check_by_id(db, artifact.check_id)
        if not check:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Check not found: {artifact.check_id}",
            )

        # Create artifact
        created_artifact = await ArtifactCoreService.create_artifact(db, artifact)

        # Return response without binary data
        return CheckArtifactResponse(
            id=created_artifact.id,
            check_id=created_artifact.check_id,
            check_result_id=created_artifact.check_result_id,
            check_result_timestamp=created_artifact.check_result_timestamp,
            artifact_type=created_artifact.artifact_type,
            content_type=created_artifact.content_type,
            filename=created_artifact.filename,
            size_bytes=created_artifact.size_bytes,
            created_at=created_artifact.created_at,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to create artifact", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create artifact: {str(e)}",
        ) from e


@router.get(
    "/artifacts/{artifact_id}",
    response_model=CheckArtifactResponse,
    summary="Get artifact metadata",
    description="Get artifact metadata (without binary data)",
    responses={
        404: {"model": ErrorResponse, "description": "Artifact not found"},
    },
)
async def get_artifact_metadata(
    artifact_id: Annotated[UUID, Path(description="Artifact UUID")],
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
):
    """Get artifact metadata without binary data."""
    artifact = await ArtifactCoreService.get_artifact_by_id(db, artifact_id)
    if not artifact:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Artifact not found: {artifact_id}",
        )

    return CheckArtifactResponse(
        id=artifact.id,
        check_id=artifact.check_id,
        check_result_id=artifact.check_result_id,
        check_result_timestamp=artifact.check_result_timestamp,
        artifact_type=artifact.artifact_type,
        content_type=artifact.content_type,
        filename=artifact.filename,
        size_bytes=artifact.size_bytes,
        created_at=artifact.created_at,
    )


@router.get(
    "/artifacts/{artifact_id}/download",
    response_class=Response,
    summary="Download artifact",
    description="Download artifact binary data",
    responses={
        200: {"description": "Artifact binary data"},
        404: {"model": ErrorResponse, "description": "Artifact not found"},
    },
)
async def download_artifact(
    artifact_id: Annotated[UUID, Path(description="Artifact UUID")],
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
):
    """Download artifact binary data."""
    artifact = await ArtifactCoreService.get_artifact_by_id(db, artifact_id)
    if not artifact:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Artifact not found: {artifact_id}",
        )

    # Return binary data with appropriate content type
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
)
async def list_check_artifacts(
    check_id: Annotated[UUID, Path(description="Check UUID")],
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
    limit: Annotated[
        int, Query(ge=1, le=500, description="Maximum number of artifacts to return")
    ] = 100,
):
    """List artifacts for a check (without binary data)."""
    # Verify check exists
    check = await CheckCoreService.get_check_by_id(db, check_id)
    if not check:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Check not found: {check_id}",
        )

    # Get artifacts (without binary data)
    artifacts = await ArtifactCoreService.list_artifacts_by_check(
        db, check_id, limit=limit, include_data=False
    )

    # Convert to response models
    artifact_responses = [
        CheckArtifactResponse(
            id=a.id,
            check_id=a.check_id,
            check_result_id=a.check_result_id,
            check_result_timestamp=a.check_result_timestamp,
            artifact_type=a.artifact_type,
            content_type=a.content_type,
            filename=a.filename,
            size_bytes=a.size_bytes,
            created_at=a.created_at,
        )
        for a in artifacts
    ]

    return CheckArtifactListResponse(
        artifacts=artifact_responses,
        total=len(artifact_responses),
        check_id=check_id,
    )


@router.get(
    "/checks/{check_id}/results/{check_result_id}/artifacts",
    response_model=CheckArtifactListResponse,
    summary="List artifacts for check result",
    description="List artifacts for a specific check execution",
    responses={
        404: {"model": ErrorResponse, "description": "Check not found"},
    },
)
async def list_check_result_artifacts(
    check_id: Annotated[UUID, Path(description="Check UUID")],
    check_result_id: Annotated[UUID, Path(description="Check result UUID")],
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
):
    """List artifacts for a specific check result."""
    # Verify check exists
    check = await CheckCoreService.get_check_by_id(db, check_id)
    if not check:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Check not found: {check_id}",
        )

    # Get artifacts for this specific result
    artifacts = await ArtifactCoreService.list_artifacts_by_check_result(
        db, check_id, check_result_id, include_data=False
    )

    # Convert to response models
    artifact_responses = [
        CheckArtifactResponse(
            id=a.id,
            check_id=a.check_id,
            check_result_id=a.check_result_id,
            check_result_timestamp=a.check_result_timestamp,
            artifact_type=a.artifact_type,
            content_type=a.content_type,
            filename=a.filename,
            size_bytes=a.size_bytes,
            created_at=a.created_at,
        )
        for a in artifacts
    ]

    return CheckArtifactListResponse(
        artifacts=artifact_responses,
        total=len(artifact_responses),
        check_id=check_id,
    )


@router.delete(
    "/artifacts/{artifact_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete artifact",
    description="Delete a specific artifact",
    responses={
        404: {"model": ErrorResponse, "description": "Artifact not found"},
    },
)
async def delete_artifact(
    artifact_id: Annotated[UUID, Path(description="Artifact UUID")],
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
):
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
    response_model=None,
    summary="Get artifact statistics",
    description="Get statistics about artifacts for a check",
    responses={
        404: {"model": ErrorResponse, "description": "Check not found"},
    },
)
async def get_artifact_stats(
    check_id: Annotated[UUID, Path(description="Check UUID")],
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
):
    """Get artifact statistics for a check."""
    # Verify check exists
    check = await CheckCoreService.get_check_by_id(db, check_id)
    if not check:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Check not found: {check_id}",
        )

    stats = await ArtifactCoreService.get_artifact_stats(db, check_id)
    return stats
