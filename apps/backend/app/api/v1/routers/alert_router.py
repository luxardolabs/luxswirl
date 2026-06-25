"""
Alert router - HTTP endpoints for alert operations.

All business logic is delegated to AlertCoreService.
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
from app.core.security import verify_api_token
from app.db import get_db
from app.schemas.alert_schema import (
    AlertCreate,
    AlertListResponse,
    AlertResponse,
    AlertUpdate,
)
from app.services.core.alert_core_service import AlertCoreService

router = APIRouter(prefix="/alerts", tags=["Alerts"])


@router.get(
    "",
    response_model=AlertListResponse,
    summary="List all alerts",
    description="Get a list of all alerts with optional filtering",
)
async def list_alerts(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
    is_enabled: Annotated[bool | None, Query(description="Filter by enabled status")] = None,
    is_global: Annotated[bool | None, Query(description="Filter by global status")] = None,
    page: Annotated[int, Query(ge=1, description="Page number")] = 1,
    page_size: Annotated[int, Query(ge=1, le=1000, description="Items per page")] = 50,
):
    """List all alerts with pagination."""
    skip = (page - 1) * page_size

    alerts, total = await AlertCoreService.list_alerts(
        db=db,
        skip=skip,
        limit=page_size,
        is_enabled=is_enabled,
        is_global=is_global,
    )

    # Convert to response models
    # TODO: Add counts for notification_provider_count and check_count
    alert_responses = [
        AlertResponse(
            id=a.id,
            name=a.name,
            description=a.description,
            trigger_type=a.trigger_type,
            trigger_config=a.trigger_config,
            is_enabled=a.is_enabled,
            is_global=a.is_global,
            notify_on_recovery=a.notify_on_recovery,
            resend_interval_minutes=a.resend_interval_minutes,
            max_resends=a.max_resends,
            custom_subject=a.custom_subject,
            custom_message=a.custom_message,
            notification_provider_count=0,  # TODO
            check_count=0,  # TODO
            created_at=a.created_at,
            updated_at=a.updated_at,
            deleted_at=a.deleted_at,
        )
        for a in alerts
    ]

    pages = (total + page_size - 1) // page_size

    return AlertListResponse(
        items=alert_responses,
        total=total,
        page=page,
        page_size=page_size,
        pages=pages,
    )


@router.get(
    "/{alert_id}",
    response_model=AlertResponse,
    summary="Get alert by UUID",
    description="Get a specific alert by its UUID",
)
async def get_alert(
    alert_id: Annotated[UUID, Path(description="Alert UUID")],
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
):
    """Get a specific alert."""
    try:
        alert = await AlertCoreService.get_alert_by_id(db, alert_id)
    except NotFoundException as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e

    return AlertResponse(
        id=alert.id,
        name=alert.name,
        description=alert.description,
        trigger_type=alert.trigger_type,
        trigger_config=alert.trigger_config,
        is_enabled=alert.is_enabled,
        is_global=alert.is_global,
        notify_on_recovery=alert.notify_on_recovery,
        resend_interval_minutes=alert.resend_interval_minutes,
        max_resends=alert.max_resends,
        custom_subject=alert.custom_subject,
        custom_message=alert.custom_message,
        notification_provider_count=0,  # TODO
        check_count=0,  # TODO
        created_at=alert.created_at,
        updated_at=alert.updated_at,
        deleted_at=alert.deleted_at,
    )


@router.post(
    "",
    response_model=AlertResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new alert",
    description="Create a new alert with the specified configuration",
)
async def create_alert(
    data: AlertCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
):
    """Create a new alert."""
    try:
        alert = await AlertCoreService.create_alert(db, data)
    except ValidationException as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from e
    except DuplicateResourceException as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e

    return AlertResponse(
        id=alert.id,
        name=alert.name,
        description=alert.description,
        trigger_type=alert.trigger_type,
        trigger_config=alert.trigger_config,
        is_enabled=alert.is_enabled,
        is_global=alert.is_global,
        notify_on_recovery=alert.notify_on_recovery,
        resend_interval_minutes=alert.resend_interval_minutes,
        max_resends=alert.max_resends,
        custom_subject=alert.custom_subject,
        custom_message=alert.custom_message,
        notification_provider_count=len(data.notification_provider_ids),
        check_count=len(data.check_ids),
        created_at=alert.created_at,
        updated_at=alert.updated_at,
        deleted_at=alert.deleted_at,
    )


@router.patch(
    "/{alert_id}",
    response_model=AlertResponse,
    summary="Update an alert",
    description="Update an existing alert",
)
async def update_alert(
    data: AlertUpdate,
    alert_id: Annotated[UUID, Path(description="Alert UUID")],
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
):
    """Update an alert."""
    try:
        alert = await AlertCoreService.update_alert(db, alert_id, data)
    except NotFoundException as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except ValidationException as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from e

    return AlertResponse(
        id=alert.id,
        name=alert.name,
        description=alert.description,
        trigger_type=alert.trigger_type,
        trigger_config=alert.trigger_config,
        is_enabled=alert.is_enabled,
        is_global=alert.is_global,
        notify_on_recovery=alert.notify_on_recovery,
        resend_interval_minutes=alert.resend_interval_minutes,
        max_resends=alert.max_resends,
        custom_subject=alert.custom_subject,
        custom_message=alert.custom_message,
        notification_provider_count=0,  # TODO
        check_count=0,  # TODO
        created_at=alert.created_at,
        updated_at=alert.updated_at,
        deleted_at=alert.deleted_at,
    )


@router.delete(
    "/{alert_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an alert",
    description="Delete an alert (soft delete by default)",
)
async def delete_alert(
    alert_id: Annotated[UUID, Path(description="Alert UUID")],
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
    hard_delete: Annotated[bool, Query(description="Permanently delete (not recommended)")] = False,
):
    """Delete an alert."""
    try:
        await AlertCoreService.delete_alert(db, alert_id, hard_delete=hard_delete)
    except NotFoundException as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e

    return None


# Notification provider mappings


@router.post(
    "/{alert_id}/notification-providers",
    status_code=status.HTTP_201_CREATED,
    summary="Add notification provider to alert",
    description="Add a notification provider to an alert",
)
async def add_notification_provider(
    alert_id: Annotated[UUID, Path(description="Alert UUID")],
    provider_id: Annotated[UUID, Query(description="Notification provider UUID")],
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
):
    """Add a notification provider to an alert."""
    try:
        mapping = await AlertCoreService.add_notification_provider(db, alert_id, provider_id)
    except NotFoundException as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except ValidationException as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from e

    return {
        "message": "Notification provider added successfully",
        "mapping_id": str(mapping.id),
    }


@router.delete(
    "/{alert_id}/notification-providers/{provider_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove notification provider from alert",
    description="Remove a notification provider from an alert",
)
async def remove_notification_provider(
    alert_id: Annotated[UUID, Path(description="Alert UUID")],
    provider_id: Annotated[UUID, Path(description="Provider UUID")],
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
):
    """Remove a notification provider from an alert."""
    try:
        await AlertCoreService.remove_notification_provider(db, alert_id, provider_id)
    except NotFoundException as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e

    return None


# Check mappings


@router.post(
    "/{alert_id}/checks",
    status_code=status.HTTP_201_CREATED,
    summary="Add check to alert",
    description="Add a check to an alert",
)
async def add_check(
    alert_id: Annotated[UUID, Path(description="Alert UUID")],
    check_id: Annotated[UUID, Query(description="Check UUID")],
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
):
    """Add a check to an alert."""
    try:
        mapping = await AlertCoreService.add_check(db, alert_id, check_id)
    except NotFoundException as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except ValidationException as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from e

    return {"message": "Check added successfully", "mapping_id": str(mapping.id)}


@router.delete(
    "/{alert_id}/checks/{check_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove check from alert",
    description="Remove a check from an alert",
)
async def remove_check(
    alert_id: Annotated[UUID, Path(description="Alert UUID")],
    check_id: Annotated[UUID, Path(description="Check UUID")],
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
):
    """Remove a check from an alert."""
    try:
        await AlertCoreService.remove_check(db, alert_id, check_id)
    except NotFoundException as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e

    return None
