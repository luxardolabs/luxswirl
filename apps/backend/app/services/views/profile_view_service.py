"""
Profile view service — context building for the user-profile UI.

Wraps user-update + change-password flows. Returns view-friendly
(kind, message) tuples that the router renders via the shared
`partials/profile/status_message.html` template. Also the panel's API tokens
(`partials/profile/api_tokens.html`): create, shown once, list and revoke.
"""

from typing import Any
from uuid import UUID

from fastapi import Request
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundException, value_error_message
from app.models.enum_model import ApiTokenAccess
from app.schemas.api_token_schema import ApiTokenCreate
from app.schemas.auth_schema import AuthedUser
from app.schemas.user_schema import UserUpdate
from app.services.core.api_token_core_service import ApiTokenCoreService
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.user_core_service import UserCoreService
from app.shared.logger import get_logger

logger = get_logger("luxswirl.web.services.profile")


class ProfileViewService:
    """View-layer wrapper for /profile endpoints."""

    @staticmethod
    async def build_panel_context(
        db: AsyncSession, request: Request, current_user: AuthedUser
    ) -> dict[str, Any]:
        """Initial-load panel context."""
        return {
            **await ProfileViewService.api_tokens_context(db, request, current_user),
            "update_url": str(request.app.url_path_for("profile.update")),
            "change_password_url": str(
                request.app.url_path_for("profile.change_password")
            ),
            # The user's own tokens' calls, loaded into the panel once it opens.
            "activity_url": str(request.app.url_path_for("profile_activity.index")),
        }

    @staticmethod
    async def api_tokens_context(
        db: AsyncSession,
        request: Request,
        current_user: AuthedUser,
        created_token: str | None = None,
        created_name: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        """The API tokens section: the user's tokens, and one just created."""
        tokens = await ApiTokenCoreService.list_tokens(db, current_user.id)
        return {
            "request": request,
            "current_user": current_user,
            "tokens": [
                {
                    "token": token,
                    "access": ApiTokenCoreService.access_of(token.scopes),
                    "revoke_url": str(
                        request.app.url_path_for(
                            "api_tokens.revoke", token_id=str(token.id)
                        )
                    ),
                }
                for token in tokens
            ],
            "create_url": str(request.app.url_path_for("api_tokens.create")),
            "created_token": created_token,
            "created_name": created_name,
            "error": error,
        }

    @staticmethod
    async def create_api_token(
        db: AsyncSession,
        request: Request,
        current_user: AuthedUser,
        name: str,
        expires_in_days: str | None,
        access: ApiTokenAccess,
    ) -> dict[str, Any]:
        """Create a token for the user; the section, showing it this once."""
        try:
            data = ApiTokenCreate(
                name=name,
                expires_in_days=int(expires_in_days) if expires_in_days else None,
                scopes=ApiTokenCoreService.scopes_for(access),
            )
        except ValidationError, ValueError:
            return await ProfileViewService.api_tokens_context(
                db,
                request,
                current_user,
                error="A token needs a name of at most 100 characters, and an expiry of 1 to 3650 days or never",
            )
        token, plaintext = await ApiTokenCoreService.create_token(
            db, current_user.id, data
        )
        return await ProfileViewService.api_tokens_context(
            db, request, current_user, created_token=plaintext, created_name=token.name
        )

    @staticmethod
    async def revoke_api_token(
        db: AsyncSession, request: Request, current_user: AuthedUser, token_id: UUID
    ) -> dict[str, Any]:
        """Revoke one of the user's tokens (an admin's: anyone's); the section."""
        try:
            await ApiTokenCoreService.revoke_token(
                db, token_id, current_user.id, current_user.role
            )
        except NotFoundException:
            return await ProfileViewService.api_tokens_context(
                db, request, current_user, error="That API token no longer exists"
            )
        return await ProfileViewService.api_tokens_context(db, request, current_user)

    @staticmethod
    async def update_profile(
        db: AsyncSession,
        user: AuthedUser,
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
            return "error", value_error_message(e)
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
        user: AuthedUser,
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
            await auth_service.change_password(
                db, user.id, current_password, new_password
            )
        except ValueError as e:
            return "error", value_error_message(e)

        logger.info(
            "User changed their password via web UI",
            extra={
                "event": "auth.password_changed",
                "username": user.username,
                "user_id": str(user.id),
            },
        )
        return "success", "Password changed successfully"
