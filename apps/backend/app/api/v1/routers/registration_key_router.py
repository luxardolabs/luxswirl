"""
Registration Key router - API endpoints for managing shared registration tokens.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_api_token
from app.db import get_db
from app.schemas.base import ErrorResponse
from app.schemas.registration_key_schema import (
    RegistrationKeyCreate,
    RegistrationKeyCreateResponse,
    RegistrationKeyListResponse,
    RegistrationKeyResponse,
    RegistrationKeyRevoke,
    RegistrationKeyUpdate,
)
from app.services.core.registration_key_core_service import RegistrationKeyCoreService

logger = get_logger("luxswirl.api.registration_keys")

router = APIRouter(prefix="/registration-keys", tags=["Registration Keys"])


@router.post(
    "",
    response_model=RegistrationKeyCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create registration key",
    description="Create a new shared registration token for agent registration and recovery",
    responses={
        201: {"description": "Registration key created successfully"},
        401: {"model": ErrorResponse, "description": "Authentication failed"},
    },
)
async def create_registration_key(
    data: RegistrationKeyCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    token: Annotated[str, Depends(verify_api_token)],
):
    """
    Create a new registration key.

    The plaintext key is only shown once in the response and cannot be retrieved later.
    Store it securely.
    """
    key, plaintext_key = await RegistrationKeyCoreService.create_key(
        db=db,
        data=data,
        created_by=None,  # Future: Get from JWT token
    )

    return RegistrationKeyCreateResponse(
        id=key.id,
        name=key.name,
        key=plaintext_key,
        message="Save this key securely - it cannot be retrieved again",
    )


@router.get(
    "",
    response_model=RegistrationKeyListResponse,
    summary="List registration keys",
    description="Get all registration keys with pagination and filtering",
    responses={
        200: {"description": "List of registration keys"},
        401: {"model": ErrorResponse, "description": "Authentication failed"},
    },
)
async def list_registration_keys(
    db: Annotated[AsyncSession, Depends(get_db)],
    token: Annotated[str, Depends(verify_api_token)],
    skip: Annotated[int, Query(ge=0, description="Number of records to skip")] = 0,
    limit: Annotated[int, Query(ge=1, le=1000, description="Maximum records to return")] = 100,
    include_revoked: Annotated[bool, Query(description="Include revoked keys")] = False,
):
    """
    List all registration keys.

    Supports pagination and filtering by revoked status.
    """
    keys, total = await RegistrationKeyCoreService.list_keys(
        db=db,
        skip=skip,
        limit=limit,
        include_revoked=include_revoked,
    )

    return RegistrationKeyListResponse(
        keys=[
            RegistrationKeyResponse(
                id=key.id,
                name=key.name,
                description=key.description,
                created_by=key.created_by,
                created_at=key.created_at,
                updated_at=key.updated_at,
                last_used_at=key.last_used_at,
                usage_count=key.usage_count,
                revoked_at=key.revoked_at,
                revoked_by=key.revoked_by,
                revoked_reason=key.revoked_reason,
                status=key.status,
            )
            for key in keys
        ],
        total=total,
    )


@router.get(
    "/{key_id}",
    response_model=RegistrationKeyResponse,
    summary="Get registration key",
    description="Get details of a specific registration key",
    responses={
        200: {"description": "Registration key details"},
        401: {"model": ErrorResponse, "description": "Authentication failed"},
        404: {"model": ErrorResponse, "description": "Registration key not found"},
    },
)
async def get_registration_key(
    key_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    token: Annotated[str, Depends(verify_api_token)],
):
    """
    Get a specific registration key by ID.

    Note: Plaintext key is never returned - it was only shown at creation time.
    """
    key = await RegistrationKeyCoreService.get_key_by_id(db, key_id)

    if not key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Registration key not found: {key_id}",
        )

    return RegistrationKeyResponse(
        id=key.id,
        name=key.name,
        description=key.description,
        created_by=key.created_by,
        created_at=key.created_at,
        updated_at=key.updated_at,
        last_used_at=key.last_used_at,
        usage_count=key.usage_count,
        revoked_at=key.revoked_at,
        revoked_by=key.revoked_by,
        revoked_reason=key.revoked_reason,
        status=key.status,
    )


@router.patch(
    "/{key_id}",
    response_model=RegistrationKeyResponse,
    summary="Update registration key",
    description="Update name or description of a registration key",
    responses={
        200: {"description": "Registration key updated"},
        401: {"model": ErrorResponse, "description": "Authentication failed"},
        404: {"model": ErrorResponse, "description": "Registration key not found"},
    },
)
async def update_registration_key(
    key_id: UUID,
    data: RegistrationKeyUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    token: Annotated[str, Depends(verify_api_token)],
):
    """
    Update a registration key's metadata (name, description).

    The key itself cannot be changed - create a new key if needed.
    """
    key = await RegistrationKeyCoreService.update_key(db, key_id, data)

    return RegistrationKeyResponse(
        id=key.id,
        name=key.name,
        description=key.description,
        created_by=key.created_by,
        created_at=key.created_at,
        updated_at=key.updated_at,
        last_used_at=key.last_used_at,
        usage_count=key.usage_count,
        revoked_at=key.revoked_at,
        revoked_by=key.revoked_by,
        revoked_reason=key.revoked_reason,
        status=key.status,
    )


@router.post(
    "/{key_id}/revoke",
    response_model=RegistrationKeyResponse,
    summary="Revoke registration key",
    description="Revoke a registration key (soft delete)",
    responses={
        200: {"description": "Registration key revoked"},
        401: {"model": ErrorResponse, "description": "Authentication failed"},
        404: {"model": ErrorResponse, "description": "Registration key not found"},
    },
)
async def revoke_registration_key(
    key_id: UUID,
    data: RegistrationKeyRevoke,
    db: Annotated[AsyncSession, Depends(get_db)],
    token: Annotated[str, Depends(verify_api_token)],
):
    """
    Revoke a registration key.

    Revoked keys can no longer be used for authentication but remain in the
    database for audit purposes.
    """
    key = await RegistrationKeyCoreService.revoke_key(
        db=db,
        key_id=key_id,
        data=data,
        revoked_by=None,  # Future: Get from JWT token
    )

    return RegistrationKeyResponse(
        id=key.id,
        name=key.name,
        description=key.description,
        created_by=key.created_by,
        created_at=key.created_at,
        updated_at=key.updated_at,
        last_used_at=key.last_used_at,
        usage_count=key.usage_count,
        revoked_at=key.revoked_at,
        revoked_by=key.revoked_by,
        revoked_reason=key.revoked_reason,
        status=key.status,
    )


@router.delete(
    "/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete registration key",
    description="Delete a registration key (soft delete by default)",
    responses={
        204: {"description": "Registration key deleted"},
        401: {"model": ErrorResponse, "description": "Authentication failed"},
        404: {"model": ErrorResponse, "description": "Registration key not found"},
    },
)
async def delete_registration_key(
    key_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    token: Annotated[str, Depends(verify_api_token)],
    hard_delete: Annotated[bool, Query(description="Permanently delete (dangerous)")] = False,
):
    """
    Delete a registration key.

    By default, this is a soft delete (revokes the key).
    Use hard_delete=true to permanently remove from database (not recommended).
    """
    await RegistrationKeyCoreService.delete_key(db, key_id, hard_delete=hard_delete)

    return None
