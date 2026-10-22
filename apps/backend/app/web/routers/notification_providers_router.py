"""
Notification Providers router — web UI for managing notification providers.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.query_params import ProviderTypeFilter
from app.db import get_db
from app.services.views.notification_providers_view_service import (
    NotificationProvidersViewService,
)
from app.web.deps import CurrentUserWeb, EditorUserWeb
from app.web.hx_responses import hx_close_panel_and_refresh, hx_empty_with_toast
from app.web.templates import templates

logger = get_logger("luxswirl.web.notification_providers")

router = APIRouter(tags=["Web UI - Notification Providers"], include_in_schema=False)


@router.get(
    "/notification-providers/create-form",
    response_class=HTMLResponse,
    name="create_provider_form",
)
async def create_provider_form(
    request: Request,
    current_user: CurrentUserWeb,
    provider_type: ProviderTypeFilter = None,
) -> HTMLResponse:
    """HTMX partial — empty 'new provider' form (or type selector)."""
    return templates.TemplateResponse(
        request,
        "partials/panels/notifications/notification_provider_form.html",
        NotificationProvidersViewService.build_create_form_context(
            request, current_user, provider_type
        ),
    )


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.get(
    "/notification-providers/{provider_id}/edit-form",
    response_class=HTMLResponse,
    name="edit_provider_form",
)
async def edit_provider_form(
    request: Request,
    provider_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: CurrentUserWeb,
) -> HTMLResponse:
    """HTMX partial — populated edit form."""
    return templates.TemplateResponse(
        request,
        "partials/panels/notifications/notification_provider_form.html",
        await NotificationProvidersViewService.build_edit_form_context(
            db, request, current_user, provider_id
        ),
    )


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.post(
    "/notification-providers/create",
    response_class=HTMLResponse,
    name="web_create_provider",
)
async def create_provider(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: EditorUserWeb,
) -> HTMLResponse:
    """Create a new notification provider."""
    form = await request.form()
    await NotificationProvidersViewService.create_provider(db, dict(form))
    return hx_close_panel_and_refresh()


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.post(
    "/notification-providers/{provider_id}/update",
    response_class=HTMLResponse,
    name="web_update_provider",
)
async def update_provider(
    request: Request,
    provider_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: EditorUserWeb,
) -> HTMLResponse:
    """Update an existing notification provider."""
    form = await request.form()
    await NotificationProvidersViewService.update_provider(db, provider_id, dict(form))
    return hx_close_panel_and_refresh()


@router.delete(
    "/notification-providers/{provider_id}",
    response_class=HTMLResponse,
    name="web_delete_provider",
)
async def delete_provider(
    request: Request,
    provider_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: EditorUserWeb,
) -> HTMLResponse:
    """Delete a notification provider (soft delete)."""
    await NotificationProvidersViewService.delete_provider(db, provider_id)
    return hx_empty_with_toast("Notification provider deleted")


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.post(
    "/notification-providers/{provider_id}/toggle",
    response_class=HTMLResponse,
    name="toggle_provider",
)
async def toggle_provider(
    request: Request,
    provider_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: EditorUserWeb,
) -> HTMLResponse:
    """Toggle notification provider enabled/disabled status."""
    await NotificationProvidersViewService.toggle_provider(db, provider_id)
    return HTMLResponse(
        content="", status_code=200, headers={"HX-Trigger": "refreshPage"}
    )


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.post(
    "/notification-providers/{provider_id}/test",
    response_class=HTMLResponse,
    name="web_test_provider",
)
async def test_provider(
    request: Request,
    provider_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: EditorUserWeb,
) -> HTMLResponse:
    """Send a test notification through this provider (toast feedback)."""
    return await NotificationProvidersViewService.test_provider(db, provider_id)
