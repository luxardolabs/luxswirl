"""
User Service - handles user CRUD operations and management.
"""

from uuid import UUID

from shared.logger import get_logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.crud.user_crud import UserCRUD
from app.models.enum_model import UserRole
from app.models.user_model import User
from app.schemas.user_schema import UserCreate, UserStatsResponse, UserUpdate
from app.services.core.auth_core_service import AuthCoreService

logger = get_logger("luxswirl.services.user")


class UserCoreService:
    """Service for user management operations."""

    def __init__(self):
        """Initialize user service."""
        self.auth_service = AuthCoreService()

    async def create_user(
        self, db: AsyncSession, user_data: UserCreate, created_by: str | None = None
    ) -> User:
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

        db.add(user)

        try:
            await db.flush()
            await db.refresh(user)
            logger.info(
                "Created user",
                extra={
                    "username": user.username,
                    "user_id": str(user.id),
                    "role": user.role,
                    "created_by": created_by or "system",
                },
            )
            return user
        except IntegrityError as e:
            # No rollback here — the ValueError propagates to get_db(), which
            # owns the transaction and rolls back on any exception.
            if "username" in str(e):
                raise ValueError(f"Username '{user_data.username}' already exists") from e
            raise ValueError(f"User creation failed: {e}") from e

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

    async def get_user_by_username(self, db: AsyncSession, username: str) -> User | None:
        """
        Get user by username.

        Args:
            db: Database session
            username: Username

        Returns:
            User object or None
        """
        return await UserCRUD.get_by_username(db, username)

    async def list_users(
        self,
        db: AsyncSession,
        skip: int = 0,
        limit: int = 100,
        role: str | None = None,
        is_active: bool | None = None,
        search: str | None = None,
    ) -> tuple[list[User], int]:
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
        return list(users), total

    async def update_user(self, db: AsyncSession, user_id: UUID, user_data: UserUpdate) -> User:
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
            await db.flush()
            await db.refresh(user)
            logger.info(
                "Updated user",
                extra={"username": user.username, "user_id": str(user_id)},
            )
            return user
        except IntegrityError as e:
            # get_db() owns the transaction and rolls back on the raised error.
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
        await db.delete(user)

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

    async def unlock_user(self, db: AsyncSession, user_id: UUID) -> User:
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

        await db.flush()
        await db.refresh(user)

        logger.info(
            "Unlocked user",
            extra={"username": user.username, "user_id": str(user_id)},
        )
        return user

    async def needs_setup(self, db: AsyncSession) -> bool:
        """
        Whether the first-run setup wizard should be shown.

        Setup is needed when there is no operator-provided admin password
        (SECURITY__INITIAL_ADMIN_PASSWORD is unset) AND no admin user exists
        yet. When an env password is configured, ``ensure_default_admin`` seeds
        the admin on startup instead, so the wizard is not needed.

        Args:
            db: Database session

        Returns:
            True if the ``/setup`` wizard should handle first admin creation.
        """
        if settings.security.initial_admin_password:
            return False
        return await UserCRUD.get_first_admin(db) is None

    async def ensure_default_admin(self, db: AsyncSession) -> User | None:
        """
        Seed the default admin from env config on startup, if configured.

        Behavior:
        - If an admin already exists, return it unchanged.
        - If ``SECURITY__INITIAL_ADMIN_PASSWORD`` is set, create the admin from
          the ``SECURITY__INITIAL_ADMIN_*`` env vars with ``must_change_password``
          enforced (unattended/automation path).
        - If it is empty, do nothing and return ``None`` — first-run admin
          creation is handled interactively by the ``/setup`` wizard (see
          ``needs_setup``). This never raises, so an empty password cannot crash
          startup.

        Args:
            db: Database session

        Returns:
            The admin user, or ``None`` when first-run setup is deferred to the wizard.
        """
        existing_admin = await UserCRUD.get_first_admin(db)

        if existing_admin:
            logger.debug("Admin user already exists")
            return existing_admin

        if not settings.security.initial_admin_password:
            logger.info(
                "No admin user and no INITIAL_ADMIN_PASSWORD configured - "
                "deferring first admin creation to the /setup wizard"
            )
            return None

        logger.warning("No admin users found - seeding default admin from config")

        default_admin = UserCreate(
            username=settings.security.initial_admin_username,
            password=settings.security.initial_admin_password,
            role=UserRole.ADMIN,
            full_name="System Administrator",
            is_active=True,
            must_change_password=True,
        )

        admin_user = await self.create_user(db, default_admin, created_by="system")
        logger.warning(
            "Created default admin user from config (MUST BE CHANGED ON FIRST LOGIN)",
            extra={
                "username": admin_user.username,
                "user_id": str(admin_user.id),
            },
        )

        return admin_user

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
