"""
User Service - handles user CRUD operations and management.
"""

from uuid import UUID

from shared.logger import get_logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.user_crud import UserCRUD
from app.models.user_model import User
from app.schemas.user_schema import (
    UserCreate,
    UserResponse,
    UserStatsResponse,
    UserUpdate,
)
from app.services.core.auth_core_service import AuthCoreService

logger = get_logger("luxswirl.services.user")


class UserCoreService:
    """Service for user management operations."""

    def __init__(self) -> None:
        """Initialize user service."""
        self.auth_service = AuthCoreService()

    async def create_user(
        self, db: AsyncSession, user_data: UserCreate, created_by: str | None = None
    ) -> UserResponse:
        """
        Create a new user.

        Args:
            db: Database session
            user_data: User creation data
            created_by: Username of admin creating this user

        Returns:
            Created User object

        Raises:
            ValueError: If username or email already exists, or password doesn't meet complexity requirements
        """
        # Validate password complexity
        valid, error_message = await self.auth_service.validate_password_complexity(
            db, user_data.password
        )
        if not valid:
            raise ValueError(error_message)

        # Hash password
        password_hash = self.auth_service.hash_password(user_data.password)

        # Create user
        user = User(
            username=user_data.username,
            password_hash=password_hash,
            role=user_data.role,
            full_name=user_data.full_name,
            is_active=user_data.is_active,
            must_change_password=user_data.must_change_password,
            created_by=created_by,
        )

        try:
            # A savepoint, so a duplicate rolls back only this insert: callers
            # turn the ValueError into a form error and the request's session
            # is still committed afterwards.
            user = await UserCRUD.add_user(db, user)
            logger.info(
                "Created user",
                extra={
                    "username": user.username,
                    "user_id": str(user.id),
                    "role": user.role,
                    "created_by": created_by or "system",
                },
            )
            return UserResponse.model_validate(user)
        except IntegrityError as e:
            if "username" in str(e):
                raise ValueError(
                    f"Username '{user_data.username}' already exists"
                ) from e
            raise ValueError(f"User creation failed: {e}") from e

    async def get_user(self, db: AsyncSession, user_id: UUID) -> UserResponse | None:
        """The user as the web seam holds it — a DTO, not the ORM row.

        get_user_by_id stays ORM: update_user, delete_user and unlock_user all
        mutate what it returns.
        """
        user = await self.get_user_by_id(db, user_id)
        return UserResponse.model_validate(user) if user else None

    async def get_user_by_id(self, db: AsyncSession, user_id: UUID) -> User | None:
        """
        Get user by ID.

        Args:
            db: Database session
            user_id: User UUID

        Returns:
            User object or None
        """
        return await UserCRUD.get_by_id(db, user_id)

    async def list_users(
        self,
        db: AsyncSession,
        skip: int = 0,
        limit: int = 100,
        role: str | None = None,
        is_active: bool | None = None,
        search: str | None = None,
    ) -> tuple[list[UserResponse], int]:
        """
        List users with optional filtering and pagination.

        Args:
            db: Database session
            skip: Number of records to skip
            limit: Maximum number of records to return
            role: Filter by role
            is_active: Filter by active status
            search: Search in username, email, or full_name

        Returns:
            Tuple of (users list, total count)
        """
        users, total = await UserCRUD.list_paginated(
            db,
            skip=skip,
            limit=limit,
            role=role,
            is_active=is_active,
            search=search,
        )
        return [UserResponse.model_validate(u) for u in users], total

    async def update_user(
        self, db: AsyncSession, user_id: UUID, user_data: UserUpdate
    ) -> UserResponse:
        """
        Update user information.

        Args:
            db: Database session
            user_id: User UUID
            user_data: User update data

        Returns:
            Updated User object

        Raises:
            ValueError: If user not found or update fails
        """
        # Get user
        user = await self.get_user_by_id(db, user_id)
        if not user:
            raise ValueError(f"User not found: {user_id}")

        # Track if role is changing (security: invalidate sessions on privilege escalation)
        original_role = user.role
        role_changed = False

        # Update fields
        update_data = user_data.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            if field == "role" and value != original_role:
                role_changed = True
            setattr(user, field, value)

        # Security: If role changed, invalidate all sessions to prevent session fixation
        # User must re-login to get session with new privileges
        if role_changed:
            session_count = await self.auth_service.logout_all_sessions(db, user_id)
            logger.warning(
                "Role changed for user - sessions invalidated, must re-login",
                extra={
                    "username": user.username,
                    "user_id": str(user.id),
                    "from_role": original_role,
                    "to_role": user.role,
                    "session_count": session_count,
                },
            )

        try:
            user = await UserCRUD.save_user_in_savepoint(db, user)
            logger.info(
                "Updated user",
                extra={"username": user.username, "user_id": str(user_id)},
            )
            return UserResponse.model_validate(user)
        except IntegrityError as e:
            raise ValueError(f"User update failed: {e}") from e

    async def delete_user(self, db: AsyncSession, user_id: UUID) -> bool:
        """
        Delete user.

        Args:
            db: Database session
            user_id: User UUID

        Returns:
            True if user was deleted

        Raises:
            ValueError: If user not found or is last admin
        """
        # Get user
        user = await self.get_user_by_id(db, user_id)
        if not user:
            raise ValueError(f"User not found: {user_id}")

        # Check if this is the last admin
        if user.role == "admin":
            admin_count = await UserCRUD.count_by_role(db, "admin")
            if admin_count <= 1:
                raise ValueError("Cannot delete the last admin user")

        # Delete user (cascade will delete sessions)
        await UserCRUD.delete_user(db, user)

        logger.info(
            "Deleted user",
            extra={"username": user.username, "user_id": str(user_id)},
        )
        return True

    async def get_user_stats(self, db: AsyncSession) -> UserStatsResponse:
        """
        Get user statistics.

        Args:
            db: Database session

        Returns:
            UserStatsResponse with counts by role and status
        """
        total_users = await UserCRUD.count_total(db)
        active_users = await UserCRUD.count_active(db)
        admin_count = await UserCRUD.count_by_role(db, "admin")
        editor_count = await UserCRUD.count_by_role(db, "editor")
        viewer_count = await UserCRUD.count_by_role(db, "viewer")
        locked_accounts = await UserCRUD.count_locked(db)

        return UserStatsResponse(
            total_users=total_users,
            active_users=active_users,
            admin_count=admin_count,
            editor_count=editor_count,
            viewer_count=viewer_count,
            locked_accounts=locked_accounts,
        )

    async def unlock_user(self, db: AsyncSession, user_id: UUID) -> UserResponse:
        """
        Unlock a locked user account.

        Args:
            db: Database session
            user_id: User UUID

        Returns:
            Updated User object

        Raises:
            ValueError: If user not found
        """
        user = await self.get_user_by_id(db, user_id)
        if not user:
            raise ValueError(f"User not found: {user_id}")

        user.locked_until = None
        user.failed_login_attempts = 0

        user = await UserCRUD.save_user(db, user)

        logger.info(
            "Unlocked user",
            extra={"username": user.username, "user_id": str(user_id)},
        )
        return UserResponse.model_validate(user)

    @staticmethod
    async def get_active_user_count(db: AsyncSession) -> int:
        """
        Get count of active users.

        Args:
            db: Database session

        Returns:
            Number of active users
        """
        return await UserCRUD.count_active(db)
