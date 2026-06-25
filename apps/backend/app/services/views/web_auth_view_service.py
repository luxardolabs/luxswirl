"""
Web auth service - handles authentication for web UI.

This service provides auth functionality specifically for web UI,
calling core auth service and formatting responses for templates.
"""

from typing import Any, cast

from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.session_model import Session
from app.models.user_model import User
from app.schemas.auth_schema import ChangePasswordRequest
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
    ) -> User | None:
        """
        Authenticate user for web login.

        Args:
            db: Database session
            username: Username
            password: Plain text password
            client_ip: Client IP for audit logging (trusted-proxy-aware)
            user_agent: User-Agent header for audit logging

        Returns:
            User if authenticated, None otherwise
        """
        return cast(
            User | None,
            await self.auth_service.authenticate_user(
                db, username, password, client_ip=client_ip, user_agent=user_agent
            ),
        )

    async def create_session(
        self,
        db: AsyncSession,
        user: User,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[Session, str]:
        """
        Create session for web user.

        Args:
            db: Database session
            user: User to create session for
            ip_address: Client IP address
            user_agent: Client user agent

        Returns:
            Tuple of (Session, token)
        """
        return cast(
            tuple[Session, str],
            await self.auth_service.create_session(
                db, user, ip_address=ip_address, user_agent=user_agent
            ),
        )

    async def verify_session(self, db: AsyncSession, token: str) -> Session | None:
        """
        Verify session token for web request.

        Args:
            db: Database session
            token: Session token

        Returns:
            Session if valid, None otherwise
        """
        return cast(Session | None, await self.auth_service.verify_session(db, token))

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
        self, db: AsyncSession, user: User, request: ChangePasswordRequest
    ) -> tuple[bool, str | None]:
        """
        Change user password.

        Args:
            db: Database session
            user: User changing password
            request: Password change request

        Returns:
            Tuple of (success, error_message)
        """
        try:
            await self.auth_service.change_password(
                db,
                user,
                current_password=request.current_password,
                new_password=request.new_password,
            )
            return True, None
        except ValueError as e:
            return False, str(e)
        except Exception:
            logger.error("Password change error", exc_info=True)
            return False, "An error occurred while changing password"

    async def get_user_sessions(self, db: AsyncSession, user: User) -> list[dict[str, Any]]:
        """
        Get user's active sessions formatted for web UI.

        Args:
            db: Database session
            user: User to get sessions for

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
    async def get_security_settings(db):
        return await SettingsCoreService.get_security_settings(db)
