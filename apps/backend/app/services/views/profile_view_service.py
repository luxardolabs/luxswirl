"""
Profile view service — context building for the user-profile UI.

Wraps user-update + change-password flows. Returns view-friendly
(kind, message) tuples that the router renders via the shared
`partials/profile/status_message.html` template.
"""

from typing import Any

from fastapi import Request
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user_model import User
from app.schemas.user_schema import UserUpdate
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.user_core_service import UserCoreService

logger = get_logger("luxswirl.web.services.profile")


class ProfileViewService:
    """View-layer wrapper for /profile endpoints."""

    @staticmethod
    def build_panel_context(request: Request, current_user: User) -> dict[str, Any]:
        """Initial-load panel context."""
        return {
            "request": request,
            "current_user": current_user,
        }

    @staticmethod
    async def update_profile(
        db: AsyncSession,
        user: User,
        full_name: str | None,
    ) -> tuple[str, str]:
        """
        Apply a profile-update form. Returns (kind, message) where
        kind ∈ {"success", "error"} for the status template.

        Translates UserCoreService errors into UI-friendly messages.
        """
        user_service = UserCoreService()
        update_data = UserUpdate(full_name=full_name if full_name else None)
        try:
            updated_user = await user_service.update_user(db, user.id, update_data)
        except ValueError as e:
            return "error", str(e)
        except Exception:
            logger.error("Error updating user", exc_info=True)
            return "error", "An error occurred while updating user"
        if not updated_user:
            return "error", "User not found"
        logger.info(
            "User updated their profile via web UI",
            extra={"username": user.username, "user_id": str(user.id)},
        )
        return "success", "Profile updated successfully"

    @staticmethod
    async def change_password(
        db: AsyncSession,
        user: User,
        current_password: str,
        new_password: str,
        confirm_password: str,
    ) -> tuple[str, str]:
        """
        Apply a change-password form. Returns (kind, message).

        AuthCoreService.change_password handles current-password verification
        and complexity rules; we layer the form-level "passwords match"
        check on top because that's a UI concern, not auth-domain logic.
        """
        if new_password != confirm_password:
            return "error", "New passwords do not match"

        auth_service = AuthCoreService()
        try:
            await auth_service.change_password(db, user, current_password, new_password)
        except ValueError as e:
            return "error", str(e)

        logger.info(
            "User changed their password via web UI",
            extra={
                "event": "auth.password_changed",
                "username": user.username,
                "user_id": str(user.id),
            },
        )
        return "success", "Password changed successfully"
