"""
Registration Key service - business logic for shared registration tokens.
"""

import secrets
from uuid import UUID

import bcrypt
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundException, ValidationException
from app.crud.registration_key_crud import RegistrationKeyCRUD
from app.models.registration_key_model import RegistrationKey
from app.schemas.registration_key_schema import (
    RegistrationKeyCreate,
    RegistrationKeyResponse,
    RegistrationKeyRevoke,
)
from app.shared.logger import get_logger

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
        # swallowed-exceptions: fail closed — any bcrypt error is a failed
        # verification, never an accepted key.
        except Exception:
            logger.exception("Error verifying registration key")
            return False

    @staticmethod
    async def create_key(
        db: AsyncSession,
        data: RegistrationKeyCreate,
        created_by: str | None = None,
    ) -> tuple[RegistrationKeyResponse, str]:
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

        key = await RegistrationKeyCRUD.add_key(db, key)

        logger.info(
            "Created registration key",
            extra={"key_name": key.name, "key_id": str(key.id)},
        )

        return RegistrationKeyResponse.model_validate(key), plaintext_key

    @staticmethod
    async def list_keys(
        db: AsyncSession,
        skip: int = 0,
        limit: int = 100,
        include_revoked: bool = False,
    ) -> tuple[list[RegistrationKeyResponse], int]:
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
        keys, total = await RegistrationKeyCRUD.list_paginated(
            db, skip=skip, limit=limit, include_revoked=include_revoked
        )
        return [RegistrationKeyResponse.model_validate(k) for k in keys], total

    @staticmethod
    async def get_key(db: AsyncSession, key_id: UUID) -> RegistrationKeyResponse:
        """One registration key's metadata, usage and status; never the key.

        Raises:
            NotFoundException: If key not found
        """
        key = await RegistrationKeyCRUD.get_by_id(db, key_id)
        if not key:
            raise NotFoundException(f"Registration key not found: {key_id}")
        return RegistrationKeyResponse.model_validate(key)

    @staticmethod
    async def revoke_key(
        db: AsyncSession,
        key_id: UUID,
        data: RegistrationKeyRevoke,
        revoked_by: str | None = None,
    ) -> RegistrationKeyResponse:
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
            ValidationException: If the key is already revoked: its revocation
                (when, by whom, why) stands as recorded
        """
        key = await RegistrationKeyCRUD.get_by_id(db, key_id)
        if not key:
            raise NotFoundException(f"Registration key not found: {key_id}")
        revoked = await RegistrationKeyCRUD.revoke(
            db, key_id, revoked_by=revoked_by, reason=data.reason
        )
        if revoked is None:
            raise ValidationException(f"Registration key {key.name} is already revoked")
        logger.info(
            "Revoked registration key",
            extra={
                "key_name": revoked.name,
                "key_id": str(revoked.id),
                "reason": data.reason,
            },
        )
        return RegistrationKeyResponse.model_validate(revoked)

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
        key = await RegistrationKeyCRUD.get_by_id(db, key_id)
        if not key:
            raise NotFoundException(f"Registration key not found: {key_id}")

        if hard_delete:
            await RegistrationKeyCRUD.hard_delete_key(db, key)
            logger.info(
                "Hard deleted registration key",
                extra={"key_name": key.name, "key_id": str(key.id)},
            )
        elif key.revoked_at is not None:
            # Already revoked: its revocation (when, by whom, why) stands.
            logger.info(
                "Registration key already revoked; kept as recorded",
                extra={"key_name": key.name, "key_id": str(key.id)},
            )
        else:
            # Soft delete (revoke)
            await RegistrationKeyCRUD.revoke(
                db, key_id, revoked_by=None, reason="Deleted by admin"
            )
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
        # bcrypt hashes are salted, so the key cannot be looked up by its hash:
        # each active key is checked in memory, and the first match is used.
        active_keys = await RegistrationKeyCRUD.list_active(db)
        match = next(
            (
                key
                for key in active_keys
                if RegistrationKeyCoreService.verify_key(plaintext_key, key.key_hash)
            ),
            None,
        )
        if match is None:
            logger.warning("Failed to verify registration key")
            return None

        # Count the use atomically; the row is returned updated.
        used = await RegistrationKeyCRUD.record_usage(db, match.id)
        if used is None:  # revoked or deleted between the read and the update
            return None
        logger.debug(
            "Verified registration key",
            extra={
                "key_name": used.name,
                "key_id": str(used.id),
                "usage_count": used.usage_count,
            },
        )
        return used
