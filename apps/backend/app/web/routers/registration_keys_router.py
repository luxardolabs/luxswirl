"""
Registration Keys router — web UI for managing registration tokens.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_deps import AdminUserWeb
from app.db import get_db
from app.services.views.registration_keys_view_service import RegistrationKeysViewService
from app.web._hx_responses import hx_empty_with_toast
from app.web.templates_config import templates

logger = get_logger("luxswirl.web.registration_keys")

router = APIRouter(tags=["Web UI - Registration Keys"], include_in_schema=False)


@router.get("/registration-keys/create-form", response_class=HTMLResponse)
async def create_key_form(
    request: Request,
    current_user: AdminUserWeb,
):
    """HTMX partial — empty 'create new key' form."""
    return templates.TemplateResponse(
        request,
        "panels/registration_keys/registration_key_form.html",
        RegistrationKeysViewService.build_create_form_context(request, current_user),
    )


@router.post("/registration-keys/create", response_class=HTMLResponse)
async def create_key(
    request: Request,
    name: Annotated[str, Form()],
    current_user: AdminUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
    description: Annotated[str | None, Form()] = None,
):
    """Create a new registration key and show it once in a modal."""
    try:
        context = await RegistrationKeysViewService.create_key(
            db, request, current_user, name, description
        )
        return templates.TemplateResponse(
            request, "panels/registration_keys/registration_key_created_panel.html", context
        )
    except Exception as e:
        logger.error("Error creating registration key", exc_info=True)
        return templates.TemplateResponse(
            request,
            "partials/error_message.html",
            RegistrationKeysViewService.build_error_context(request, current_user, str(e)),
            status_code=400,
        )


@router.post("/registration-keys/{key_id}/revoke", response_class=HTMLResponse)
async def revoke_key(
    request: Request,
    key_id: UUID,
    current_user: AdminUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
    reason: Annotated[str | None, Form()] = None,
):
    """Revoke a registration key."""
    try:
        await RegistrationKeysViewService.revoke_key(db, key_id, reason)
        return HTMLResponse(content="", status_code=200, headers={"HX-Trigger": "refreshPage"})
    except Exception as e:
        logger.error("Error revoking registration key", exc_info=True)
        return templates.TemplateResponse(
            request,
            "partials/error_message.html",
            RegistrationKeysViewService.build_error_context(request, current_user, str(e)),
            status_code=400,
        )


@router.delete("/registration-keys/{key_id}", response_class=HTMLResponse)
async def delete_key(
    request: Request,
    key_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: AdminUserWeb,
):
    """Delete a registration key (soft delete)."""
    try:
        await RegistrationKeysViewService.delete_key(db, key_id)
        return hx_empty_with_toast("Registration key deleted")
    except Exception as e:
        logger.error("Error deleting registration key", exc_info=True)
        return templates.TemplateResponse(
            request,
            "partials/error_message.html",
            RegistrationKeysViewService.build_error_context(request, current_user, str(e)),
            status_code=400,
        )
