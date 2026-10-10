"""
Auth Router - API endpoints for authentication and session management.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AdminUser, CurrentUser
from app.core.config import settings
from app.core.rate_limit import limiter
from app.core.request_helpers import client_ip_from_request
from app.db import get_db
from app.schemas.auth_schema import (
    ChangePasswordRequest,
    CurrentUserResponse,
    LoginRequest,
    LoginResponse,
    SessionListResponse,
    SessionResponse,
)
from app.schemas.base_schema import MessageResponse, SessionsTerminatedResponse
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.settings_core_service import SettingsCoreService

router = APIRouter(prefix="/auth", tags=["Authentication"])
logger = get_logger("luxswirl.api.routers.auth")


@router.post("/login", response_model=LoginResponse, status_code=status.HTTP_200_OK)
@limiter.limit(settings.security.login_rate_limit)
async def login(
    login_data: LoginRequest,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> LoginResponse:
    """
    Login with username and password.

    Returns session cookie (HTTP-only, Secure, SameSite=Lax).

    **Security Features:**
    - Rate limiting to prevent brute-force attacks
    - Account locking after 5 failed attempts (30 min lockout)
    - Secure session tokens (256-bit entropy, SHA-256 hashed)
    - HTTP-only cookies (immune to XSS)
    - 7-day session expiration
    """
    auth_service = AuthCoreService()

    # Capture request context up-front (trusted-proxy-aware)
    client_ip = client_ip_from_request(request)
    user_agent = request.headers.get("user-agent")

    # Authenticate user
    user = await auth_service.authenticate_user(
        db,
        login_data.username,
        login_data.password,
        client_ip=client_ip,
        user_agent=user_agent,
    )

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    # Check if password must be changed
    if user.must_change_password:
        # Return special response indicating password change required
        # Frontend should redirect to change password page
        return LoginResponse(
            message="Password change required",
            user=CurrentUserResponse.model_validate(user),
            must_change_password=True,
        )

    # Create session
    session, token = await auth_service.create_session(
        db, user.id, ip_address=client_ip, user_agent=user_agent
    )

    # Get session lifetime from database security settings
    security_settings = await SettingsCoreService.get_security_settings(db)
    session_lifetime_days = security_settings.get("session_lifetime_days", 7)
    max_age_seconds = session_lifetime_days * 24 * 60 * 60

    # Get cookie configuration from settings
    cookie_config = settings.security

    # Set HTTP-only cookie
    response.set_cookie(
        key=cookie_config.session_cookie_name,
        value=token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=max_age_seconds,
        path=cookie_config.session_cookie_path,
    )

    logger.info(
        "User logged in successfully",
        extra={
            "event": "auth.login.success",
            "username": user.username,
            "user_id": str(user.id),
        },
    )

    return LoginResponse(
        message="Login successful", user=CurrentUserResponse.model_validate(user)
    )


@router.post(
    "/logout",
    status_code=status.HTTP_200_OK,
    response_model=MessageResponse,
)
async def logout(
    response: Response,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    session_token: str | None = None,
) -> MessageResponse:
    """
    Logout current user and invalidate session.

    Deletes session from database and clears cookie.
    """
    auth_service = AuthCoreService()

    # Get session token from cookie (get_current_user already validated it)
    # We need to extract it from the request

    async def get_session_token(
        session_token: Annotated[
            str | None, Cookie(alias=settings.security.session_cookie_name)
        ] = None,
    ) -> str | None:
        return session_token

    # Logout (delete session)
    if session_token:
        await auth_service.logout(db, session_token)

    # Clear cookie
    response.delete_cookie(
        key=settings.security.session_cookie_name,
        path=settings.security.session_cookie_path,
    )

    logger.info(
        "User logged out",
        extra={
            "event": "auth.logout",
            "username": user.username,
            "user_id": str(user.id),
        },
    )

    return MessageResponse(message="Logged out successfully")


@router.get("/me", response_model=CurrentUserResponse)
async def get_current_user_info(
    user: CurrentUser,
) -> CurrentUserResponse:
    """
    Get current authenticated user information.

    Requires valid session cookie.
    """
    return CurrentUserResponse.model_validate(user)


@router.post(
    "/change-password",
    status_code=status.HTTP_200_OK,
    response_model=MessageResponse,
)
@limiter.limit(settings.security.login_rate_limit)
async def change_password(
    request: Request,
    password_data: ChangePasswordRequest,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageResponse:
    """
    Change current user's password.

    Requires current password for verification.

    **Password Requirements:**
    - Minimum 8 characters
    - At least one uppercase letter
    - At least one lowercase letter
    - At least one digit
    """
    auth_service = AuthCoreService()

    try:
        await auth_service.change_password(
            db, user.id, password_data.current_password, password_data.new_password
        )
        logger.info(
            "User changed their password",
            extra={
                "event": "auth.password_changed",
                "username": user.username,
                "user_id": str(user.id),
            },
        )
        return MessageResponse(message="Password changed successfully")
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e


@router.get("/sessions", response_model=SessionListResponse)
async def list_my_sessions(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SessionListResponse:
    """
    List all active sessions for current user.

    Shows session ID, creation time, last activity, IP address, and user agent.
    Useful for security auditing and managing active sessions.
    """
    auth_service = AuthCoreService()
    sessions = await auth_service.get_user_sessions(db, user.id)

    return SessionListResponse(
        sessions=[SessionResponse.model_validate(s) for s in sessions],
        total=len(sessions),
    )


@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_200_OK,
    response_model=MessageResponse,
)
async def logout_session(
    session_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageResponse:
    """
    Logout a specific session (revoke access).

    Users can manage their own active sessions.
    Useful for logging out other devices.

    Args:
        session_id: UUID of session to logout
    """

    auth_service = AuthCoreService()

    try:
        session_uuid = UUID(session_id)
        success = await auth_service.logout_session_by_id(db, user.id, session_uuid)

        if success:
            logger.info(
                "User logged out specific session",
                extra={
                    "event": "auth.logout_session",
                    "username": user.username,
                    "user_id": str(user.id),
                    "session_id": str(session_id),
                },
            )
            return MessageResponse(message="Session logged out successfully")
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Session not found or does not belong to you",
            )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid session ID format",
        ) from None


@router.post(
    "/logout-all",
    status_code=status.HTTP_200_OK,
    response_model=SessionsTerminatedResponse,
)
async def logout_all_sessions(
    response: Response,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SessionsTerminatedResponse:
    """
    Logout all sessions for current user.

    Useful when:
    - User suspects account compromise
    - User wants to force logout from all devices
    - Password was just changed

    This will log out the current session too!
    """
    auth_service = AuthCoreService()

    count = await auth_service.logout_all_sessions(db, user.id)

    # Clear current session cookie
    response.delete_cookie(
        key=settings.security.session_cookie_name,
        path=settings.security.session_cookie_path,
    )

    logger.info(
        "User logged out all sessions",
        extra={
            "event": "auth.logout_all",
            "username": user.username,
            "user_id": str(user.id),
            "session_count": count,
        },
    )

    return SessionsTerminatedResponse(
        message=f"Logged out from all devices ({count} sessions)",
        sessions_terminated=count,
    )


# Admin-only endpoints


@router.get("/sessions/user/{user_id}", response_model=SessionListResponse)
async def list_user_sessions_admin(
    user_id: UUID,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SessionListResponse:
    """
    List all sessions for a specific user (admin only).

    Args:
        user_id: UUID of user

    Requires admin role.
    """

    auth_service = AuthCoreService()

    sessions = await auth_service.get_user_sessions(db, user_id)

    return SessionListResponse(
        sessions=[SessionResponse.model_validate(s) for s in sessions],
        total=len(sessions),
    )


@router.post(
    "/sessions/user/{user_id}/logout-all",
    status_code=status.HTTP_200_OK,
    response_model=SessionsTerminatedResponse,
)
async def logout_user_sessions_admin(
    user_id: UUID,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SessionsTerminatedResponse:
    """
    Force logout all sessions for a user (admin only).

    Useful when:
    - Disabling a user account
    - Security incident response
    - Password reset

    Args:
        user_id: UUID of user

    Requires admin role.
    """

    auth_service = AuthCoreService()

    count = await auth_service.logout_all_sessions(db, user_id)

    logger.info(
        "Admin force-logged out all sessions for user",
        extra={
            "actor_username": admin.username,
            "target_user_id": str(user_id),
            "session_count": count,
        },
    )

    return SessionsTerminatedResponse(
        message=f"Logged out all sessions for user ({count} total)",
        sessions_terminated=count,
    )
