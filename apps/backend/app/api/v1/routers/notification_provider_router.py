"""
Notification Provider router - HTTP endpoints for notification provider operations.

All business logic is delegated to NotificationCoreService.
This router only handles HTTP concerns (request/response, status codes, etc.).
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    DuplicateResourceException,
    NotFoundException,
    ValidationException,
)
from app.core.query_params import ProviderTypeFilter
from app.core.security import verify_api_token
from app.db import get_db
from app.schemas.notification_provider_schema import (
    NotificationProviderCreate,
    NotificationProviderListResponse,
    NotificationProviderResponse,
    NotificationProviderSchemaResponse,
    NotificationProviderTestRequest,
    NotificationProviderTestResponse,
    NotificationProviderTypesResponse,
    NotificationProviderUpdate,
)
from app.services.core.notification_core_service import NotificationCoreService

router = APIRouter(prefix="/notification-providers", tags=["Notification Providers"])


@router.get(
    "/types",
    response_model=NotificationProviderTypesResponse,
    summary="Get available provider types",
    description="Get list of all available notification provider types with their configuration schemas",
)
async def get_provider_types(
    _: Annotated[str, Depends(verify_api_token)],
):
    """Get available notification provider types."""
    providers = NotificationCoreService.get_available_provider_types()

    return NotificationProviderTypesResponse(
        providers=[
            NotificationProviderSchemaResponse(
                provider_type=p["type"],
                provider_name=p["name"],
                provider_description=p["description"],
                config_schema=p["schema"],
            )
            for p in providers
        ]
    )


@router.get(
    "",
    response_model=NotificationProviderListResponse,
    summary="List all notification providers",
    description="Get a list of all notification providers with optional filtering",
)
async def list_providers(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
    provider_type: ProviderTypeFilter = None,
    page: Annotated[int, Query(ge=1, description="Page number")] = 1,
    page_size: Annotated[int, Query(ge=1, le=1000, description="Items per page")] = 50,
):
    """List all notification providers with pagination."""
    skip = (page - 1) * page_size

    providers, total = await NotificationCoreService.list_providers(
        db=db,
        skip=skip,
        limit=page_size,
        provider_type=provider_type,
    )

    # Convert to response models
    provider_responses = [
        NotificationProviderResponse(
            id=p.id,
            provider_type=p.provider_type,
            friendly_name=p.friendly_name,
            config=p.config,  # Will be masked by schema validator
            is_default_enabled=p.is_default_enabled,
            rate_limit_count=p.rate_limit_count,
            rate_limit_window_minutes=p.rate_limit_window_minutes,
            created_at=p.created_at,
            updated_at=p.updated_at,
            deleted_at=p.deleted_at,
        )
        for p in providers
    ]

    pages = (total + page_size - 1) // page_size

    return NotificationProviderListResponse(
        items=provider_responses,
        total=total,
        page=page,
        page_size=page_size,
        pages=pages,
    )


@router.get(
    "/{provider_id}",
    response_model=NotificationProviderResponse,
    summary="Get notification provider by UUID",
    description="Get a specific notification provider by its UUID",
)
async def get_provider(
    provider_id: Annotated[UUID, Path(description="Provider UUID")],
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
):
    """Get a specific notification provider."""
    try:
        provider = await NotificationCoreService.get_provider_by_id(db, provider_id)
    except NotFoundException as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e

    return NotificationProviderResponse(
        id=provider.id,
        provider_type=provider.provider_type,
        friendly_name=provider.friendly_name,
        config=provider.config,
        is_default_enabled=provider.is_default_enabled,
        rate_limit_count=provider.rate_limit_count,
        rate_limit_window_minutes=provider.rate_limit_window_minutes,
        created_at=provider.created_at,
        updated_at=provider.updated_at,
        deleted_at=provider.deleted_at,
    )


@router.post(
    "",
    response_model=NotificationProviderResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new notification provider",
    description="Create a new notification provider with the specified configuration",
)
async def create_provider(
    data: NotificationProviderCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
):
    """Create a new notification provider."""
    try:
        provider = await NotificationCoreService.create_provider(db, data)
    except ValidationException as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from e
    except DuplicateResourceException as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e

    return NotificationProviderResponse(
        id=provider.id,
        provider_type=provider.provider_type,
        friendly_name=provider.friendly_name,
        config=provider.config,
        is_default_enabled=provider.is_default_enabled,
        rate_limit_count=provider.rate_limit_count,
        rate_limit_window_minutes=provider.rate_limit_window_minutes,
        created_at=provider.created_at,
        updated_at=provider.updated_at,
        deleted_at=provider.deleted_at,
    )


@router.patch(
    "/{provider_id}",
    response_model=NotificationProviderResponse,
    summary="Update a notification provider",
    description="Update an existing notification provider",
)
async def update_provider(
    data: NotificationProviderUpdate,
    provider_id: Annotated[UUID, Path(description="Provider UUID")],
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
):
    """Update a notification provider."""
    try:
        provider = await NotificationCoreService.update_provider(db, provider_id, data)
    except NotFoundException as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except ValidationException as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from e

    return NotificationProviderResponse(
        id=provider.id,
        provider_type=provider.provider_type,
        friendly_name=provider.friendly_name,
        config=provider.config,
        is_default_enabled=provider.is_default_enabled,
        rate_limit_count=provider.rate_limit_count,
        rate_limit_window_minutes=provider.rate_limit_window_minutes,
        created_at=provider.created_at,
        updated_at=provider.updated_at,
        deleted_at=provider.deleted_at,
    )


@router.delete(
    "/{provider_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a notification provider",
    description="Delete a notification provider (soft delete by default)",
)
async def delete_provider(
    provider_id: Annotated[UUID, Path(description="Provider UUID")],
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
    hard_delete: Annotated[bool, Query(description="Permanently delete (not recommended)")] = False,
):
    """Delete a notification provider."""
    try:
        await NotificationCoreService.delete_provider(db, provider_id, hard_delete=hard_delete)
    except NotFoundException as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e

    return None


@router.post(
    "/{provider_id}/test",
    response_model=NotificationProviderTestResponse,
    summary="Test a notification provider",
    description="Send a test notification through the specified provider",
)
async def test_provider(
    provider_id: Annotated[UUID, Path(description="Provider UUID")],
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
    data: NotificationProviderTestRequest = NotificationProviderTestRequest(),
):
    """Test a notification provider by sending a test notification."""
    try:
        success, error = await NotificationCoreService.test_provider(
            db,
            provider_id,
            test_message=data.test_message,
        )
    except NotFoundException as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e

    if success:
        return NotificationProviderTestResponse(
            success=True,
            message="Test notification sent successfully",
            error=None,
        )
    else:
        return NotificationProviderTestResponse(
            success=False,
            message="Test notification failed",
            error=error,
        )
