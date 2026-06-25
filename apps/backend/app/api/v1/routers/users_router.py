"""
Users Router - API endpoints for user management (admin only).
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_deps import AdminUser
from app.db import get_db
from app.schemas.user_schema import (
    UserCreate,
    UserListResponse,
    UserPasswordReset,
    UserResponse,
    UserStatsResponse,
    UserUpdate,
)
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.user_core_service import UserCoreService

router = APIRouter(prefix="/users", tags=["User Management"])
logger = get_logger("luxswirl.api.routers.users")


@router.get("", response_model=UserListResponse)
async def list_users(
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    skip: Annotated[int, Query(ge=0, description="Number of records to skip")] = 0,
    limit: Annotated[int, Query(ge=1, le=200, description="Maximum records to return")] = 50,
    role: Annotated[str | None, Query(description="Filter by role")] = None,
    is_active: Annotated[bool | None, Query(description="Filter by active status")] = None,
    search: Annotated[str | None, Query(description="Search username or name")] = None,
):
    """
    List all users with optional filtering and pagination.

    **Query Parameters:**
    - skip: Pagination offset
    - limit: Page size (1-200)
    - role: Filter by role (admin, editor, viewer)
    - is_active: Filter by active status
    - search: Search in username or full name

    Requires admin role.
    """
    user_service = UserCoreService()
    users, total = await user_service.list_users(
        db, skip=skip, limit=limit, role=role, is_active=is_active, search=search
    )

    return UserListResponse(
        users=[UserResponse.model_validate(u) for u in users],
        total=total,
        page=skip // limit + 1,
        page_size=limit,
    )


@router.get("/stats", response_model=UserStatsResponse)
async def get_user_stats(
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Get user statistics.

    Returns counts by role, status, etc.

    Requires admin role.
    """
    user_service = UserCoreService()
    return await user_service.get_user_stats(db)


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: UUID,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Get user by ID.

    Args:
        user_id: User UUID

    Requires admin role.
    """

    user_service = UserCoreService()

    user = await user_service.get_user_by_id(db, user_id)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User not found: {user_id}",
        )

    return UserResponse.model_validate(user)


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    user_data: UserCreate,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Create a new user.

    **Password Requirements:**
    - Minimum 8 characters
    - At least one uppercase letter
    - At least one lowercase letter
    - At least one digit

    Requires admin role.
    """
    user_service = UserCoreService()

    try:
        user = await user_service.create_user(db, user_data, created_by=admin.username)
        logger.info(
            "Admin created user",
            extra={
                "actor_username": admin.username,
                "target_username": user.username,
                "target_user_id": str(user.id),
                "role": user.role,
            },
        )
        return UserResponse.model_validate(user)

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e


@router.patch("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: UUID,
    user_data: UserUpdate,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Update user information.

    **Updatable Fields:**
    - role (admin, editor, viewer)
    - full_name
    - is_active (activate/deactivate account)
    - must_change_password

    Args:
        user_id: User UUID

    Requires admin role.
    """

    user_service = UserCoreService()

    try:
        user = await user_service.update_user(db, user_id, user_data)

        logger.info(
            "Admin updated user",
            extra={
                "actor_username": admin.username,
                "target_username": user.username,
                "target_user_id": str(user.id),
            },
        )
        return UserResponse.model_validate(user)

    except ValueError as e:
        if "not found" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(e),
            ) from e
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e


@router.delete("/{user_id}", status_code=status.HTTP_200_OK)
async def delete_user(
    user_id: UUID,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Delete a user.

    **Important:**
    - Cannot delete the last admin user
    - All user sessions will be terminated
    - This action is permanent

    Args:
        user_id: User UUID

    Requires admin role.
    """

    user_service = UserCoreService()

    # Prevent admin from deleting themselves
    try:
        if admin.id == user_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot delete your own account",
            )

        success = await user_service.delete_user(db, user_id)

        if success:
            logger.warning(
                "Admin deleted user",
                extra={
                    "actor_username": admin.username,
                    "target_user_id": str(user_id),
                },
            )
            return {"message": "User deleted successfully"}

    except ValueError as e:
        if "invalid" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            ) from e
        if "not found" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(e),
            ) from e
        if "last admin" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            ) from e
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e


@router.post("/{user_id}/reset-password", status_code=status.HTTP_200_OK)
async def reset_user_password(
    user_id: UUID,
    password_data: UserPasswordReset,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Reset a user's password (admin only).

    **Security:**
    - New password must meet strength requirements
    - User can be forced to change password on next login
    - All user sessions will be terminated

    Args:
        user_id: User UUID

    Requires admin role.
    """

    user_service = UserCoreService()
    auth_service = AuthCoreService()

    try:
        user = await user_service.get_user_by_id(db, user_id)

        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User not found: {user_id}",
            )

        # Reset password
        await auth_service.reset_password(
            db,
            user,
            password_data.new_password,
            force_change=password_data.must_change_password,
        )

        # Logout all sessions for security
        await auth_service.logout_all_sessions(db, user_id)

        logger.warning(
            "Admin reset password for user",
            extra={
                "actor_username": admin.username,
                "target_username": user.username,
                "target_user_id": str(user.id),
            },
        )

        return {"message": "Password reset successfully. All user sessions have been terminated."}

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e


@router.post("/{user_id}/unlock", response_model=UserResponse)
async def unlock_user(
    user_id: UUID,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Unlock a locked user account.

    Locked accounts are automatically locked after too many failed login attempts.
    This endpoint allows admin to manually unlock them.

    Args:
        user_id: User UUID

    Requires admin role.
    """

    user_service = UserCoreService()

    try:
        user = await user_service.unlock_user(db, user_id)

        logger.info(
            "Admin unlocked user",
            extra={
                "actor_username": admin.username,
                "target_username": user.username,
                "target_user_id": str(user.id),
            },
        )
        return UserResponse.model_validate(user)

    except ValueError as e:
        if "not found" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(e),
            ) from e
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e
