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
from app.core.query_params import EnabledFilter, GlobalFilter
from app.core.security import verify_api_token
from app.db import get_db
from app.schemas.alert_schema import (
    AlertCreate,
    AlertListResponse,
    AlertResponse,
    AlertUpdate,
)
from app.schemas.base_schema import MappingCreatedResponse
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
    is_enabled: EnabledFilter = None,
    is_global: GlobalFilter = None,
    page: Annotated[int, Query(ge=1, description="Page number")] = 1,
    per_page: Annotated[int, Query(ge=1, le=1000, description="Items per page")] = 50,
) -> AlertListResponse:
    """List all alerts with pagination."""
    return await AlertCoreService.list_alerts_page(
        db, page=page, per_page=per_page, is_enabled=is_enabled, is_global=is_global
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
) -> AlertResponse:
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
) -> AlertResponse:
    """Create a new alert."""
    try:
        alert = await AlertCoreService.create_alert(db, data)
    except ValidationException as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
        ) from e
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
) -> AlertResponse:
    """Update an alert."""
    try:
        alert = await AlertCoreService.update_alert(db, alert_id, data)
    except NotFoundException as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except ValidationException as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
        ) from e

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
    hard_delete: Annotated[
        bool, Query(description="Permanently delete (not recommended)")
    ] = False,
) -> None:
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
    response_model=MappingCreatedResponse,
)
async def add_notification_provider(
    alert_id: Annotated[UUID, Path(description="Alert UUID")],
    provider_id: Annotated[UUID, Query(description="Notification provider UUID")],
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
) -> MappingCreatedResponse:
    """Add a notification provider to an alert."""
    try:
        mapping = await AlertCoreService.add_notification_provider(
            db, alert_id, provider_id
        )
    except NotFoundException as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except ValidationException as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
        ) from e

    return MappingCreatedResponse(
        message="Notification provider added successfully", mapping_id=mapping.id
    )


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
) -> None:
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
    response_model=MappingCreatedResponse,
)
async def add_check(
    alert_id: Annotated[UUID, Path(description="Alert UUID")],
    check_id: Annotated[UUID, Query(description="Check UUID")],
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[str, Depends(verify_api_token)],
) -> MappingCreatedResponse:
    """Add a check to an alert."""
    try:
        mapping = await AlertCoreService.add_check(db, alert_id, check_id)
    except NotFoundException as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except ValidationException as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
        ) from e

    return MappingCreatedResponse(
        message="Check added successfully", mapping_id=mapping.id
    )


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
) -> None:
    """Remove a check from an alert."""
    try:
        await AlertCoreService.remove_check(db, alert_id, check_id)
    except NotFoundException as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e

    return None
