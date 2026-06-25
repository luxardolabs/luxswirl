"""
Profile router — web UI for user profile management.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_deps import CurrentUserWeb
from app.db import get_db
from app.services.views.profile_view_service import ProfileViewService
from app.web.routers._render import status_message
from app.web.templates_config import templates

logger = get_logger("luxswirl.web.routers.profile")

router = APIRouter(tags=["Web UI - Profile"])


@router.get("/profile", response_class=HTMLResponse, include_in_schema=False)
async def profile_panel(
    request: Request,
    user: CurrentUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """User profile panel (HTMX)."""
    return templates.TemplateResponse(
        request,
        "partials/profile_panel.html",
        ProfileViewService.build_panel_context(request, user),
    )


@router.post("/profile/update", response_class=HTMLResponse, include_in_schema=False)
async def update_profile(
    request: Request,
    user: CurrentUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
    full_name: Annotated[str | None, Form()] = None,
):
    """Update user profile information."""
    kind, message = await ProfileViewService.update_profile(db, user, full_name)
    return status_message(
        request,
        "partials/profile/status_message.html",
        kind,
        message,
        status_code=200 if kind == "success" else 400,
    )


@router.post("/profile/change-password", response_class=HTMLResponse, include_in_schema=False)
async def change_password(
    request: Request,
    current_password: Annotated[str, Form()],
    new_password: Annotated[str, Form()],
    confirm_password: Annotated[str, Form()],
    user: CurrentUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Change user password."""
    kind, message = await ProfileViewService.change_password(
        db, user, current_password, new_password, confirm_password
    )
    return status_message(
        request,
        "partials/profile/status_message.html",
        kind,
        message,
        status_code=200 if kind == "success" else 400,
    )
