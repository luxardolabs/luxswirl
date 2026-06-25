"""
Notification Providers router — web UI for managing notification providers.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_deps import (
    CurrentUserWeb,
    EditorUserWeb,
)
from app.db import get_db
from app.services.views.notification_providers_view_service import (
    NotificationProvidersViewService,
)
from app.web._hx_responses import hx_empty_with_toast
from app.web.routers._render import error_partial
from app.web.templates_config import templates

logger = get_logger("luxswirl.web.notification_providers")

router = APIRouter(tags=["Web UI - Notification Providers"], include_in_schema=False)


@router.get("/notification-providers/create-form", response_class=HTMLResponse)
async def create_provider_form(
    request: Request,
    current_user: CurrentUserWeb,
    provider_type: str | None = None,
):
    """HTMX partial — empty 'new provider' form (or type selector)."""
    try:
        return templates.TemplateResponse(
            request,
            "panels/notifications/notification_provider_form.html",
            NotificationProvidersViewService.build_create_form_context(
                request, current_user, provider_type
            ),
        )
    except Exception as e:
        logger.error("Error rendering provider create form", exc_info=True)
        return error_partial(request, current_user, str(e), 500)


@router.get("/notification-providers/{provider_id}/edit-form", response_class=HTMLResponse)
async def edit_provider_form(
    request: Request,
    provider_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUserWeb,
):
    """HTMX partial — populated edit form."""
    try:
        return templates.TemplateResponse(
            request,
            "panels/notifications/notification_provider_form.html",
            await NotificationProvidersViewService.build_edit_form_context(
                db, request, current_user, provider_id
            ),
        )
    except Exception as e:
        logger.error("Error rendering provider edit form", exc_info=True)
        return error_partial(request, current_user, str(e), 500)


@router.post("/notification-providers/create", response_class=HTMLResponse)
async def create_provider(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
):
    """Create a new notification provider."""
    try:
        form = await request.form()
        await NotificationProvidersViewService.create_provider(db, dict(form))
        return HTMLResponse(
            content="",
            status_code=200,
            headers={"HX-Trigger": "closeSidePanel,refreshPage"},
        )
    except Exception as e:
        logger.error("Error creating provider", exc_info=True)
        return error_partial(request, current_user, str(e), 400)


@router.post("/notification-providers/{provider_id}/update", response_class=HTMLResponse)
async def update_provider(
    request: Request,
    provider_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
):
    """Update an existing notification provider."""
    try:
        form = await request.form()
        await NotificationProvidersViewService.update_provider(db, provider_id, dict(form))
        return HTMLResponse(
            content="",
            status_code=200,
            headers={"HX-Trigger": "closeSidePanel,refreshPage"},
        )
    except Exception as e:
        logger.error("Error updating provider", exc_info=True)
        return error_partial(request, current_user, str(e), 400)


@router.delete("/notification-providers/{provider_id}", response_class=HTMLResponse)
async def delete_provider(
    request: Request,
    provider_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
):
    """Delete a notification provider (soft delete)."""
    try:
        await NotificationProvidersViewService.delete_provider(db, provider_id)
        return hx_empty_with_toast("Notification provider deleted")
    except Exception as e:
        logger.error("Error deleting provider", exc_info=True)
        return error_partial(request, current_user, str(e), 400)


@router.post("/notification-providers/{provider_id}/toggle", response_class=HTMLResponse)
async def toggle_provider(
    request: Request,
    provider_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
):
    """Toggle notification provider enabled/disabled status."""
    try:
        await NotificationProvidersViewService.toggle_provider(db, provider_id)
        return HTMLResponse(content="", status_code=200, headers={"HX-Trigger": "refreshPage"})
    except Exception as e:
        logger.error("Error toggling provider", exc_info=True)
        return error_partial(request, current_user, str(e), 400)


@router.post("/notification-providers/{provider_id}/test", response_class=HTMLResponse)
async def test_provider(
    request: Request,
    provider_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
):
    """Send a test notification through this provider (toast feedback)."""
    try:
        success, error_msg = await NotificationProvidersViewService.test_provider(db, provider_id)
        if success:
            return hx_empty_with_toast("Test notification sent successfully!")
        return hx_empty_with_toast(f"Test failed: {error_msg}", kind="error")
    except Exception as e:
        logger.error("Error testing provider", exc_info=True)
        return hx_empty_with_toast(str(e), kind="error")
