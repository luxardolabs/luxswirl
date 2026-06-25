"""
Registration Key service - business logic for shared registration tokens.
"""

import secrets
from uuid import UUID

import bcrypt
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_utils import utc_now
from app.core.exceptions import NotFoundException
from app.crud.registration_key_crud import RegistrationKeyCRUD
from app.models.registration_key_model import RegistrationKey
from app.schemas.registration_key_schema import (
    RegistrationKeyCreate,
    RegistrationKeyRevoke,
    RegistrationKeyUpdate,
)

logger = get_logger("luxswirl.services.registration_key")


class RegistrationKeyCoreService:
    """Service for managing shared registration tokens."""

    @staticmethod
    def generate_key() -> str:
        """
        Generate a secure random registration key.

        Format: luxswirl_rk_{32 random hex chars}
        Total length: 42 characters

        Returns:
            Securely generated key
        """
        random_part = secrets.token_hex(16)  # 32 hex chars
        return f"luxswirl_rk_{random_part}"

    @staticmethod
    def hash_key(key: str) -> str:
        """
        Hash a key using bcrypt.

        Args:
            key: Plaintext key to hash

        Returns:
            Bcrypt hash
        """
        salt = bcrypt.gensalt()
        hashed = bcrypt.hashpw(key.encode("utf-8"), salt)
        return hashed.decode("utf-8")

    @staticmethod
    def verify_key(key: str, key_hash: str) -> bool:
        """
        Verify a key against its hash.

        Args:
            key: Plaintext key to verify
            key_hash: Bcrypt hash to check against

        Returns:
            True if key matches hash
        """
        try:
            return bcrypt.checkpw(key.encode("utf-8"), key_hash.encode("utf-8"))
        except Exception:
            logger.error("Error verifying registration key", exc_info=True)
            return False

    @staticmethod
    async def create_key(
        db: AsyncSession,
        data: RegistrationKeyCreate,
        created_by: str | None = None,
    ) -> tuple[RegistrationKey, str]:
        """
        Create a new registration key.

        Args:
            db: Database session
            data: Key creation data
            created_by: Admin user creating the key

        Returns:
            Tuple of (RegistrationKey model, plaintext key)
            Note: Plaintext key is only returned here and never stored
        """
        # Generate key
        plaintext_key = RegistrationKeyCoreService.generate_key()

        # Hash key
        key_hash = RegistrationKeyCoreService.hash_key(plaintext_key)

        # Create model
        key = RegistrationKey(
            name=data.name,
            description=data.description,
            key_hash=key_hash,
            created_by=created_by,
        )

        db.add(key)
        await db.flush()
        await db.refresh(key)

        logger.info(
            "Created registration key",
            extra={"key_name": key.name, "key_id": str(key.id)},
        )

        return key, plaintext_key

    @staticmethod
    async def get_key_by_id(
        db: AsyncSession,
        key_id: UUID,
    ) -> RegistrationKey | None:
        """
        Get a registration key by ID.

        Args:
            db: Database session
            key_id: Key UUID

        Returns:
            RegistrationKey model or None
        """
        return await RegistrationKeyCRUD.get_by_id(db, key_id)

    @staticmethod
    async def list_keys(
        db: AsyncSession,
        skip: int = 0,
        limit: int = 100,
        include_revoked: bool = False,
    ) -> tuple[list[RegistrationKey], int]:
        """
        List registration keys with pagination.

        Args:
            db: Database session
            skip: Number of records to skip
            limit: Maximum number of records to return
            include_revoked: Whether to include revoked keys

        Returns:
            Tuple of (list of keys, total count)
        """
        return await RegistrationKeyCRUD.list_paginated(
            db, skip=skip, limit=limit, include_revoked=include_revoked
        )

    @staticmethod
    async def update_key(
        db: AsyncSession,
        key_id: UUID,
        data: RegistrationKeyUpdate,
    ) -> RegistrationKey:
        """
        Update a registration key.

        Args:
            db: Database session
            key_id: Key UUID
            data: Update data

        Returns:
            Updated RegistrationKey model

        Raises:
            NotFoundException: If key not found
        """
        key = await RegistrationKeyCoreService.get_key_by_id(db, key_id)
        if not key:
            raise NotFoundException(f"Registration key not found: {key_id}")

        # Update fields
        if data.name is not None:
            key.name = data.name

        if data.description is not None:
            key.description = data.description

        await db.flush()
        await db.refresh(key)

        logger.info(
            "Updated registration key",
            extra={"key_name": key.name, "key_id": str(key.id)},
        )

        return key

    @staticmethod
    async def revoke_key(
        db: AsyncSession,
        key_id: UUID,
        data: RegistrationKeyRevoke,
        revoked_by: str | None = None,
    ) -> RegistrationKey:
        """
        Revoke a registration key.

        Args:
            db: Database session
            key_id: Key UUID
            data: Revocation data (reason)
            revoked_by: Admin user revoking the key

        Returns:
            Revoked RegistrationKey model

        Raises:
            NotFoundException: If key not found
        """
        key = await RegistrationKeyCoreService.get_key_by_id(db, key_id)
        if not key:
            raise NotFoundException(f"Registration key not found: {key_id}")

        # Revoke key
        key.revoked_at = utc_now()
        key.revoked_by = revoked_by
        key.revoked_reason = data.reason

        await db.flush()
        await db.refresh(key)

        logger.info(
            "Revoked registration key",
            extra={
                "key_name": key.name,
                "key_id": str(key.id),
                "reason": data.reason,
            },
        )

        return key

    @staticmethod
    async def delete_key(
        db: AsyncSession,
        key_id: UUID,
        hard_delete: bool = False,
    ) -> None:
        """
        Delete a registration key.

        Args:
            db: Database session
            key_id: Key UUID
            hard_delete: If True, permanently delete; if False, soft delete (revoke)

        Raises:
            NotFoundException: If key not found
        """
        key = await RegistrationKeyCoreService.get_key_by_id(db, key_id)
        if not key:
            raise NotFoundException(f"Registration key not found: {key_id}")

        if hard_delete:
            await db.delete(key)
            logger.info(
                "Hard deleted registration key",
                extra={"key_name": key.name, "key_id": str(key.id)},
            )
        else:
            # Soft delete (revoke)
            key.revoked_at = utc_now()
            key.revoked_reason = "Deleted by admin"
            logger.info(
                "Soft deleted (revoked) registration key",
                extra={"key_name": key.name, "key_id": str(key.id)},
            )

    @staticmethod
    async def verify_key_and_update_usage(
        db: AsyncSession,
        plaintext_key: str,
    ) -> RegistrationKey | None:
        """
        Verify a key and update its usage statistics.

        Args:
            db: Database session
            plaintext_key: Plaintext key to verify

        Returns:
            RegistrationKey model if valid, None if invalid
        """
        # Get all active keys
        active_keys = await RegistrationKeyCRUD.list_active(db)

        # Check each key
        for key in active_keys:
            if RegistrationKeyCoreService.verify_key(plaintext_key, key.key_hash):
                # Update usage stats
                key.last_used_at = utc_now()
                key.usage_count += 1
                await db.flush()
                await db.refresh(key)

                logger.debug(
                    "Verified registration key",
                    extra={
                        "key_name": key.name,
                        "key_id": str(key.id),
                        "usage_count": key.usage_count,
                    },
                )
                return key

        # No match found
        logger.warning("Failed to verify registration key")
        return None
