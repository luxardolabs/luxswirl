"""
Auth router - web UI for login/logout.

Renders HTML pages and handles form submissions.
"""

from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_deps import OptionalUserWeb
from app.core.config import settings
from app.core.rate_limit import limiter
from app.core.request_helpers import client_ip_from_request
from app.db import get_db
from app.schemas.auth_schema import ChangePasswordRequest
from app.services.views.web_auth_view_service import WebAuthViewService
from app.services.views.web_users_view_service import WebUsersViewService
from app.web.templates_config import templates

logger = get_logger("luxswirl.web.routers.auth")

router = APIRouter(tags=["Web UI - Auth"])


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(
    request: Request,
    user: OptionalUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
    redirect: str | None = None,
):
    """
    Login page.

    If first-run setup is pending, redirect to the setup wizard.
    If already authenticated, redirect to home.
    """
    # First run: no admin exists yet -> send to the setup wizard.
    if await WebUsersViewService().needs_setup(db):
        return RedirectResponse(url="/setup", status_code=status.HTTP_302_FOUND)

    # If already logged in, redirect to home or specified redirect
    if user:
        redirect_url = redirect or "/"
        return RedirectResponse(url=redirect_url, status_code=status.HTTP_302_FOUND)

    return templates.TemplateResponse(
        request,
        "pages/login.html",
        {
            "current_user": user,  # Will be None for anonymous users
            "redirect": redirect or "/",
            "error": None,
        },
    )


@router.post("/login", response_class=HTMLResponse, include_in_schema=False)
@limiter.limit(settings.security.login_rate_limit)
async def login_submit(
    request: Request,
    response: Response,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    db: Annotated[AsyncSession, Depends(get_db)],
    redirect: Annotated[str, Form()] = "/",
):
    """
    Handle login form submission.

    Returns HTML response with error or redirects on success.
    Rate limited to prevent brute-force attacks.
    """
    web_auth = WebAuthViewService()

    # Capture request context up-front for both success and failure paths.
    client_ip = client_ip_from_request(request)
    user_agent = request.headers.get("user-agent")

    # Authenticate user
    user = await web_auth.authenticate_user(
        db, username, password, client_ip=client_ip, user_agent=user_agent
    )

    if not user:
        # Authentication failed - re-render login page with error
        return templates.TemplateResponse(
            request,
            "pages/login.html",
            {
                "current_user": None,  # Not authenticated
                "redirect": redirect,
                "error": "Invalid username or password",
                "username": username,  # Pre-fill username,
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    # Create session
    session, token = await web_auth.create_session(
        db, user, ip_address=client_ip, user_agent=user_agent
    )

    logger.info(
        "User logged in successfully via web UI",
        extra={
            "event": "auth.login.success",
            "username": user.username,
            "user_id": str(user.id),
            "client_ip": client_ip,
            "user_agent": user_agent,
        },
    )

    # Get session lifetime from database security settings
    security_settings = await WebAuthViewService().get_security_settings(db)
    session_lifetime_days = security_settings.get("session_lifetime_days", 7)
    max_age_seconds = session_lifetime_days * 24 * 60 * 60

    # Get cookie configuration from settings
    cookie_config = settings.security

    # Check if password must be changed
    if user.must_change_password:
        # Set cookie first
        response = RedirectResponse(
            url="/change-password?required=true", status_code=status.HTTP_302_FOUND
        )
        response.set_cookie(
            key=cookie_config.session_cookie_name,
            value=token,
            httponly=cookie_config.session_cookie_httponly,
            secure=cookie_config.session_cookie_secure,
            samesite=cast(Literal["lax", "strict", "none"], cookie_config.session_cookie_samesite),
            max_age=max_age_seconds,
            path=cookie_config.session_cookie_path,
        )
        return response

    # Successful login - set cookie and redirect
    response = RedirectResponse(url=redirect, status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        key=cookie_config.session_cookie_name,
        value=token,
        httponly=cookie_config.session_cookie_httponly,
        secure=cookie_config.session_cookie_secure,
        samesite=cast(Literal["lax", "strict", "none"], cookie_config.session_cookie_samesite),
        max_age=max_age_seconds,
        path=cookie_config.session_cookie_path,
    )

    return response


@router.get("/logout", include_in_schema=False)
@router.post("/logout", include_in_schema=False)
async def logout(
    request: Request,
    response: Response,
    user: OptionalUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
    session_token: str | None = None,
):
    """
    Logout current user.

    Accepts both GET and POST.
    Deletes session and clears cookie, then redirects to login.
    """
    if user and session_token:
        web_auth = WebAuthViewService()

        # Get session token from cookie

        # Extract token manually from request
        cookie_token = request.cookies.get("session_token")
        if cookie_token:
            await web_auth.logout(db, cookie_token)
            logger.info(
                "User logged out via web UI",
                extra={
                    "event": "auth.logout",
                    "username": user.username,
                    "user_id": str(user.id),
                },
            )

    # Clear cookie and redirect to login
    response = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    response.delete_cookie(
        key=settings.security.session_cookie_name,
        path=settings.security.session_cookie_path,
    )

    return response


@router.get("/change-password", response_class=HTMLResponse, include_in_schema=False)
async def change_password_page(
    request: Request,
    user: OptionalUserWeb,
    required: bool = False,
):
    """
    Change password page.

    If required=true, user must change password before accessing system.
    """
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    return templates.TemplateResponse(
        request,
        "pages/change_password.html",
        {
            "current_user": user,
            "user": user,
            "required": required,
            "error": None,
            "success": None,
        },
    )


@router.post("/change-password", response_class=HTMLResponse, include_in_schema=False)
async def change_password_submit(
    request: Request,
    current_password: Annotated[str, Form()],
    new_password: Annotated[str, Form()],
    confirm_password: Annotated[str, Form()],
    user: OptionalUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
    required: Annotated[bool, Form()] = False,
):
    """
    Handle change password form submission.
    """
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    # Validate passwords match
    if new_password != confirm_password:
        return templates.TemplateResponse(
            request,
            "pages/change_password.html",
            {
                "current_user": user,
                "user": user,
                "required": required,
                "error": "New passwords do not match",
                "success": None,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # Change password
    web_auth = WebAuthViewService()

    try:
        request_data = ChangePasswordRequest(
            current_password=current_password,
            new_password=new_password,
            confirm_password=confirm_password,
        )
        success, error = await web_auth.change_password(db, user, request_data)

        if not success:
            raise ValueError(error)

        logger.info(
            "User changed password via web UI",
            extra={
                "event": "auth.password_changed",
                "username": user.username,
                "user_id": str(user.id),
            },
        )

        # If password change was required, redirect to home
        if required or user.must_change_password:
            response = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
            return response

        # Otherwise show success message
        return templates.TemplateResponse(
            request,
            "pages/change_password.html",
            {
                "current_user": user,
                "user": user,
                "required": False,
                "error": None,
                "success": "Password changed successfully",
            },
        )

    except ValueError as e:
        return templates.TemplateResponse(
            request,
            "pages/change_password.html",
            {
                "current_user": user,
                "user": user,
                "required": required,
                "error": str(e),
                "success": None,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )
