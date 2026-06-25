"""
Registration keys view service — context building for the admin
registration-keys management UI.
"""

from typing import Any
from uuid import UUID

from fastapi import Request
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user_model import User
from app.schemas.registration_key_schema import RegistrationKeyCreate, RegistrationKeyRevoke
from app.services.core.registration_key_core_service import RegistrationKeyCoreService

logger = get_logger("luxswirl.web.services.registration_keys")


class RegistrationKeysViewService:
    """View-layer wrapper for registration-key admin endpoints."""

    @staticmethod
    def build_create_form_context(request: Request, current_user: User) -> dict[str, Any]:
        """Empty-form partial context for the 'new key' modal."""
        return {
            "request": request,
            "current_user": current_user,
            "key": None,  # None signals "create mode" to the template
        }

    @staticmethod
    async def create_key(
        db: AsyncSession,
        request: Request,
        current_user: User,
        name: str,
        description: str | None,
    ) -> dict[str, Any]:
        """
        Create a key (delegates to core) and build the
        'created — copy this once' panel context with the plaintext value.
        """
        data = RegistrationKeyCreate(name=name, description=description if description else None)
        key, plaintext_key = await RegistrationKeyCoreService.create_key(db, data)
        logger.info(
            "Created registration key",
            extra={"key_name": key.name, "key_id": str(key.id)},
        )
        return {
            "request": request,
            "current_user": current_user,
            "key": key,
            "plaintext_key": plaintext_key,
        }

    @staticmethod
    async def revoke_key(db: AsyncSession, key_id: UUID, reason: str | None) -> None:
        """Revoke a key — pure delegation."""
        await RegistrationKeyCoreService.revoke_key(
            db, key_id, RegistrationKeyRevoke(reason=reason)
        )
        logger.info(
            "Revoked registration key",
            extra={"key_id": str(key_id)},
        )

    @staticmethod
    async def delete_key(db: AsyncSession, key_id: UUID) -> None:
        """Soft-delete a key — pure delegation."""
        await RegistrationKeyCoreService.delete_key(db, key_id, hard_delete=False)
        logger.info(
            "Deleted registration key",
            extra={"key_id": str(key_id)},
        )

    @staticmethod
    def build_error_context(request: Request, current_user: User, error: str) -> dict[str, Any]:
        """Common error-partial context (used by all four endpoints)."""
        return {
            "request": request,
            "current_user": current_user,
            "error": error,
        }
