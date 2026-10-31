"""
Profile router — web UI for user profile management.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.rate_limit import limiter
from app.db import get_db
from app.models.enum_model import ApiTokenAccess
from app.services.views.profile_view_service import ProfileViewService
from app.shared.logger import get_logger
from app.web.deps import CurrentUserWeb
from app.web.render import profile_status_message
from app.web.templates import templates

logger = get_logger("luxswirl.web.routers.profile")

router = APIRouter(tags=["Web UI - Profile"])


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.get(
    "/profile",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="profile.show",
)
async def profile_panel(
    request: Request,
    user: CurrentUserWeb,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> HTMLResponse:
    """User profile panel (HTMX)."""
    return templates.TemplateResponse(
        request,
        "partials/profile_panel.html",
        await ProfileViewService.build_panel_context(db, request, user),
    )


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.post(
    "/profile/api-tokens",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="api_tokens.create",
)
async def create_api_token(
    request: Request,
    user: CurrentUserWeb,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    name: Annotated[str, Form()],
    expires_in_days: Annotated[str | None, Form()] = None,
    access: Annotated[ApiTokenAccess, Form()] = ApiTokenAccess.READ,
) -> HTMLResponse:
    """Create an API token for the user; it is shown this once."""
    return templates.TemplateResponse(
        request,
        "partials/profile/api_tokens.html",
        await ProfileViewService.create_api_token(
            db, request, user, name, expires_in_days, access
        ),
    )


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.post(
    "/profile/api-tokens/{token_id}/revoke",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="api_tokens.revoke",
)
async def revoke_api_token(
    request: Request,
    token_id: UUID,
    user: CurrentUserWeb,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> HTMLResponse:
    """Revoke one of the user's API tokens."""
    return templates.TemplateResponse(
        request,
        "partials/profile/api_tokens.html",
        await ProfileViewService.revoke_api_token(db, request, user, token_id),
    )


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.post(
    "/profile/update",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="profile.update",
)
async def update_profile(
    request: Request,
    user: CurrentUserWeb,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    full_name: Annotated[str | None, Form()] = None,
) -> Response:
    """Update user profile information."""
    kind, message = await ProfileViewService.update_profile(db, user, full_name)
    return profile_status_message(
        request,
        kind,
        message,
        status_code=200 if kind == "success" else 400,
    )


@router.post(
    "/profile/change-password",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="profile.change_password",
)
@limiter.limit(settings.security.password_reset_rate_limit)
async def change_password(
    request: Request,
    current_password: Annotated[str, Form()],
    new_password: Annotated[str, Form()],
    confirm_password: Annotated[str, Form()],
    user: CurrentUserWeb,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> Response:
    """Change user password."""
    kind, message = await ProfileViewService.change_password(
        db, user, current_password, new_password, confirm_password
    )
    return profile_status_message(
        request,
        kind,
        message,
        status_code=200 if kind == "success" else 400,
    )
