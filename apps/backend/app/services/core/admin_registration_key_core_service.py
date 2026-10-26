"""
Admin registration key core service - agent registration keys as the admin API
serves them.

An admin's alone, as in the web UI. Creation and revocation record the token's
user as who created or revoked the key.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.admin_registration_key_schema import (
    AdminRegistrationKeyCreate,
    AdminRegistrationKeyRevoke,
    RegistrationKeyCreated,
    RegistrationKeyPage,
)
from app.schemas.pagination_schema import build_pagination
from app.schemas.registration_key_schema import RegistrationKeyResponse
from app.services.core.registration_key_core_service import RegistrationKeyCoreService


class AdminRegistrationKeyCoreService:
    """List, read, create and revoke agent registration keys."""

    @staticmethod
    async def list_keys(
        db: AsyncSession, *, page: int, per_page: int, include_revoked: bool
    ) -> RegistrationKeyPage:
        """One page of keys, newest first; revoked ones only when asked."""
        page = max(1, page)
        keys, total = await RegistrationKeyCoreService.list_keys(
            db,
            skip=(page - 1) * per_page,
            limit=per_page,
            include_revoked=include_revoked,
        )
        return RegistrationKeyPage.of(
            keys, build_pagination(page=page, per_page=per_page, total=total)
        )

    @staticmethod
    async def get_key(db: AsyncSession, key_id: UUID) -> RegistrationKeyResponse:
        return await RegistrationKeyCoreService.get_key(db, key_id)

    @staticmethod
    async def create_key(
        db: AsyncSession, username: str, data: AdminRegistrationKeyCreate
    ) -> RegistrationKeyCreated:
        """A new key, with its plaintext this once."""
        key, plaintext = await RegistrationKeyCoreService.create_key(
            db, data, created_by=username
        )
        return RegistrationKeyCreated(**key.model_dump(), key=plaintext)

    @staticmethod
    async def revoke_key(
        db: AsyncSession,
        username: str,
        key_id: UUID,
        data: AdminRegistrationKeyRevoke,
    ) -> RegistrationKeyResponse:
        """Revoke the key: no agent registers or recovers its key with it again."""
        return await RegistrationKeyCoreService.revoke_key(
            db, key_id, data, revoked_by=username
        )
