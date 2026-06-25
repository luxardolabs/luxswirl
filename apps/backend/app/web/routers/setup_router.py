"""
Setup router - first-run wizard for creating the initial admin account.

Shown only when no admin exists yet and no admin password was provided via
``SECURITY__INITIAL_ADMIN_PASSWORD`` (see ``WebUsersViewService.needs_setup``). This
gives a secure-by-default first run: no default credentials ship with the app;
the operator creates the first admin interactively. Unattended/automation
deploys can skip the wizard by setting the env password instead.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.services.views.web_users_view_service import WebUsersViewService
from app.web.routers._render import render_setup

logger = get_logger("luxswirl.web.routers.setup")

router = APIRouter(tags=["Web UI - Setup"])


@router.get("/setup", response_class=HTMLResponse, include_in_schema=False)
async def setup_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """
    First-run setup form.

    Redirects to /login when setup is not needed (an admin already exists or
    one is configured via env), to avoid exposing the wizard after first run.
    """
    if not await WebUsersViewService().needs_setup(db):
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    return render_setup(request)


@router.post("/setup", response_class=HTMLResponse, include_in_schema=False)
async def create_first_admin(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    confirm_password: Annotated[str, Form()],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """
    Create the first admin user.

    Only valid while setup is needed (re-checked to guard against a race or a
    replayed POST). The admin sets their own password, so no forced change is
    required afterwards.
    """
    users = WebUsersViewService()

    # Re-check before creating - guards against double-submit / race.
    if not await users.needs_setup(db):
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    username = username.strip()

    if password != confirm_password:
        return render_setup(
            request,
            username=username,
            error="Passwords do not match.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    user, error = await users.create_user(
        db,
        username=username,
        password=password,
        role="admin",
        full_name="Administrator",
        is_active=True,
        must_change_password=False,
        created_by="setup",
    )
    if error or user is None:
        return render_setup(
            request,
            username=username,
            error=error or "Could not create the admin account.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    logger.warning(
        "First admin user created via setup wizard",
        extra={
            "event": "auth.setup.admin_created",
            "username": user.username,
            "user_id": str(user.id),
        },
    )

    # get_db() commits on clean return; redirect to login to sign in.
    return RedirectResponse(url="/login?setup_complete=1", status_code=status.HTTP_302_FOUND)
