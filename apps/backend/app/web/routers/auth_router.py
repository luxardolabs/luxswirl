"""
Auth router - web UI for login/logout.

Renders HTML pages and handles form submissions.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.rate_limit import limiter
from app.core.request_helpers import client_ip_from_request
from app.core.safe_redirect import safe_redirect_target
from app.core.same_origin import is_cross_site
from app.db import get_db
from app.services.views.admin_bootstrap_view_service import AdminBootstrapViewService
from app.services.views.web_auth_view_service import WebAuthViewService
from app.web.deps import OptionalUserWeb
from app.web.templates import templates

logger = get_logger("luxswirl.web.routers.auth")

router = APIRouter(tags=["Web UI - Auth"])


@router.get(
    "/login", response_class=HTMLResponse, include_in_schema=False, name="login_page"
)
async def login_page(
    request: Request,
    user: OptionalUserWeb,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    redirect: str | None = None,
) -> Response:
    """
    Login page.

    If first-run setup is pending, redirect to the setup wizard.
    If already authenticated, redirect to home.
    """
    # First run: no user exists yet -> the token-gated /setup.
    setup_path = await AdminBootstrapViewService().login_page_redirect(request, db)
    if setup_path:
        return RedirectResponse(url=setup_path, status_code=status.HTTP_302_FOUND)

    # Where to land after sign-in: the requested page when it is on this site,
    # else home (resolved by name).
    target = safe_redirect_target(
        redirect, default=request.app.url_path_for("root_redirect")
    )

    # Already signed in: go there now.
    if user:
        return RedirectResponse(url=target, status_code=status.HTTP_302_FOUND)

    return templates.TemplateResponse(
        request,
        "pages/login.html",
        {
            "current_user": user,  # Will be None for anonymous users
            "redirect": target,
            "error": None,
            # The form pre-fills this after a failed POST; the GET must name it
            # too, or the page it renders has no username field to fill.
            "username": "",
            "page_title": "Sign In",
        },
    )


@router.post(
    "/login", response_class=HTMLResponse, include_in_schema=False, name="login_submit"
)
@limiter.limit(settings.security.login_rate_limit)
async def login_submit(
    request: Request,
    response: Response,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    redirect: Annotated[str | None, Form()] = None,
) -> Response:
    """
    Handle login form submission.

    Returns HTML response with error or redirects on success.
    Rate limited to prevent brute-force attacks.
    """
    web_auth = WebAuthViewService()
    # Where to land after sign-in: the requested page when it is on this site,
    # else home (resolved by name).
    target = safe_redirect_target(
        redirect, default=request.app.url_path_for("root_redirect")
    )

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
                "redirect": target,
                "error": "Invalid username or password",
                "username": username,  # Pre-fill username,
                "page_title": "Sign In",
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    # Create session
    token = await web_auth.create_session(
        db, user.id, ip_address=client_ip, user_agent=user_agent
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

    max_age_seconds = await WebAuthViewService.get_session_cookie_max_age(db)

    # Get cookie configuration from settings
    cookie_config = settings.security

    # Check if password must be changed
    if user.must_change_password:
        # Set cookie first
        change_password = request.app.url_path_for("change_password_page")
        response = RedirectResponse(
            url=f"{change_password}?required=true", status_code=status.HTTP_302_FOUND
        )
        response.set_cookie(
            key=cookie_config.session_cookie_name,
            value=token,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=max_age_seconds,
            path=cookie_config.session_cookie_path,
        )
        return response

    # Successful login - set cookie and redirect (only ever to a page on this site)
    response = RedirectResponse(url=target, status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        key=cookie_config.session_cookie_name,
        value=token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=max_age_seconds,
        path=cookie_config.session_cookie_path,
    )

    return response


@router.get("/logout", include_in_schema=False, name="web_logout")
@router.post("/logout", include_in_schema=False, name="web_logout")
async def logout(
    request: Request,
    user: OptionalUserWeb,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> Response:
    """Log out (GET or POST): end the session on the server, then go to /login.

    The cross-site guard covers the POST; a GET from another site (a link or an
    image there) is refused here, so a hostile page cannot sign the visitor out.
    """
    headers = {key.lower(): value for key, value in request.headers.items()}
    if is_cross_site(headers, settings.same_origin_hosts):
        return Response(
            "Cross-site request refused", status_code=status.HTTP_403_FORBIDDEN
        )
    return await WebAuthViewService().logout_response(db, request, user)


@router.get(
    "/change-password",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="change_password_page",
)
async def change_password_page(
    request: Request,
    user: OptionalUserWeb,
    required: bool = False,
) -> Response:
    """
    Change password page.

    If required=true, user must change password before accessing system.
    """
    if not user:
        return RedirectResponse(
            url=request.app.url_path_for("login_page"),
            status_code=status.HTTP_302_FOUND,
        )

    return templates.TemplateResponse(
        request,
        "pages/change_password.html",
        {
            "current_user": user,
            "user": user,
            "required": required,
            "error": None,
            "success": None,
            "page_title": "Change Password",
        },
    )


@router.post(
    "/change-password",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="change_password_submit",
)
# Same limit as /login, because it does the same thing: it verifies
# `current_password`. Unlimited attempts here is unlimited guessing of a
# logged-in user's password, from one address.
@limiter.limit(settings.security.login_rate_limit)
async def change_password_submit(
    request: Request,
    current_password: Annotated[str, Form()],
    new_password: Annotated[str, Form()],
    confirm_password: Annotated[str, Form()],
    user: OptionalUserWeb,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    required: Annotated[bool, Form()] = False,
) -> Response:
    """
    Handle change password form submission.
    """
    if not user:
        return RedirectResponse(
            url=request.app.url_path_for("login_page"),
            status_code=status.HTTP_302_FOUND,
        )

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
                "page_title": "Change Password",
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # Change password
    web_auth = WebAuthViewService()

    try:
        success, error = await web_auth.change_password(
            db, user, current_password=current_password, new_password=new_password
        )

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
            return RedirectResponse(
                url=request.app.url_path_for("root_redirect"),
                status_code=status.HTTP_302_FOUND,
            )

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
                "page_title": "Change Password",
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
                "page_title": "Change Password",
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )
