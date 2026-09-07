"""
Web auth service - handles authentication for web UI.

This service provides auth functionality specifically for web UI,
calling core auth service and formatting responses for templates.
"""

from typing import Any, cast
from uuid import UUID

from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_deps import AuthedUser
from app.schemas.auth_schema import ChangePasswordRequest, CurrentUserDTO
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.settings_core_service import SettingsCoreService

logger = get_logger("luxswirl.web.services.web_auth")


class WebAuthViewService:
    """Service for web UI authentication."""

    def __init__(self):
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
        return CurrentUserDTO.model_validate(user) if user else None

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

    async def logout(self, db: AsyncSession, token: str) -> bool:
        """
        Logout user (delete session).

        Args:
            db: Database session
            token: Session token

        Returns:
            True if session was deleted
        """
        return cast(bool, await self.auth_service.logout(db, token))

    async def change_password(
        self, db: AsyncSession, user: AuthedUser, request: ChangePasswordRequest
    ) -> tuple[bool, str | None]:
        """
        Change user password.

        Args:
            db: Database session
            user: The authenticated principal changing their password
            request: Password change request

        Returns:
            Tuple of (success, error_message)
        """
        try:
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
