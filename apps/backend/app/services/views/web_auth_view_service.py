"""
Web auth service - handles authentication for web UI.

This service provides auth functionality specifically for web UI,
calling core auth service and formatting responses for templates.
"""

from typing import Any
from uuid import UUID

from fastapi import Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.schemas.auth_schema import AuthedUser, ChangePasswordRequest, CurrentUserDTO
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.settings_core_service import SettingsCoreService
from app.shared.logger import get_logger

logger = get_logger("luxswirl.web.services.web_auth")


class WebAuthViewService:
    """Service for web UI authentication."""

    def __init__(self) -> None:
        self.auth_service = AuthCoreService()

    async def authenticate_user(
        self,
        db: AsyncSession,
        username: str,
        password: str,
        client_ip: str | None = None,
        user_agent: str | None = None,
    ) -> CurrentUserDTO | None:
        """
        Authenticate user for web login.

        Args:
            db: Database session
            username: Username
            password: Plain text password
            client_ip: Client IP for audit logging (trusted-proxy-aware)
            user_agent: User-Agent header for audit logging

        Returns:
            The authenticated principal as the web seam holds it, or None.
        """
        user = await self.auth_service.authenticate_user(
            db, username, password, client_ip=client_ip, user_agent=user_agent
        )
        return (
            CurrentUserDTO.model_validate(user, from_attributes=True) if user else None
        )

    async def create_session(
        self,
        db: AsyncSession,
        user_id: UUID,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> str:
        """
        Create session for web user and return the cookie token.

        The Session row itself stays below the seam — the caller sets a cookie,
        which needs the token and nothing else.

        Args:
            db: Database session
            user_id: The authenticated user's id
            ip_address: Client IP address
            user_agent: Client user agent

        Returns:
            The plain session token to set as the cookie value.
        """
        _, token = await self.auth_service.create_session(
            db, user_id, ip_address=ip_address, user_agent=user_agent
        )
        return token

    async def logout_response(
        self, db: AsyncSession, request: Request, user: AuthedUser | None
    ) -> RedirectResponse:
        """End the caller's session on the server, clear its cookie, go to /login.

        The session is read from the configured session cookie and deleted, so a
        copy of that cookie stops working too; clearing the browser's cookie
        alone left the session valid.
        """
        cookie = settings.security.session_cookie_name
        token = request.cookies.get(cookie)
        if token and await self.auth_service.logout(db, token) and user:
            logger.info(
                "User logged out via web UI",
                extra={
                    "event": "auth.logout",
                    "username": user.username,
                    "user_id": str(user.id),
                },
            )
        response = RedirectResponse(
            url=request.app.url_path_for("login_page"),
            status_code=status.HTTP_302_FOUND,
        )
        response.delete_cookie(key=cookie, path=settings.security.session_cookie_path)
        return response

    async def change_password(
        self,
        db: AsyncSession,
        user: AuthedUser,
        *,
        current_password: str,
        new_password: str,
    ) -> tuple[bool, str | None]:
        """
        Change user password.

        The form values go through ChangePasswordRequest (its strength rules)
        and then the core service (the configured complexity policy).

        Args:
            db: Database session
            user: The authenticated principal changing their password
            current_password: The password the user typed as current
            new_password: The requested new password

        Returns:
            Tuple of (success, error_message)
        """
        try:
            request = ChangePasswordRequest(
                current_password=current_password, new_password=new_password
            )
            await self.auth_service.change_password(
                db,
                user.id,
                current_password=request.current_password,
                new_password=request.new_password,
            )
            return True, None
        except ValueError as e:
            return False, str(e)

    async def get_user_sessions(
        self, db: AsyncSession, user: AuthedUser
    ) -> list[dict[str, Any]]:
        """
        Get user's active sessions formatted for web UI.

        No web surface renders these yet — the shaping lives here unwired,
        waiting on a sessions page (the is_current flag below is what is
        missing). The API's own listing is separate and does its own shaping.

        Args:
            db: Database session
            user: The authenticated principal whose sessions to list

        Returns:
            List of session dicts for display
        """
        sessions = await self.auth_service.get_user_sessions(db, user.id)

        return [
            {
                "id": str(session.id),
                "ip_address": session.ip_address or "Unknown",
                "user_agent": session.user_agent or "Unknown",
                "created_at": session.created_at,
                "last_activity_at": session.last_activity_at or session.created_at,
                "expires_at": session.expires_at,
                "is_current": False,  # TODO: Compare with current token
            }
            for session in sessions
        ]

    @staticmethod
    async def get_session_cookie_max_age(db: AsyncSession) -> int:
        """Session cookie Max-Age in seconds, from the configured lifetime in days.

        The days→seconds conversion and the fallback are presentation concerns:
        core stores the operator's setting, the web seam turns it into the value
        the Set-Cookie header wants.
        """
        security_settings = await SettingsCoreService.get_security_settings(db)
        lifetime_days = security_settings.get("session_lifetime_days", 7)
        return int(lifetime_days) * 24 * 60 * 60
