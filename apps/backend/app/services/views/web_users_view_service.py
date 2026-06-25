"""
Web users service - handles user management for web UI.

This service provides user management functionality specifically for web UI,
calling core user service and formatting responses for templates.
"""

from typing import cast
from uuid import UUID

from pydantic import ValidationError
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_utils import utc_now
from app.models.enum_model import UserRole, options_for
from app.models.user_model import User
from app.schemas.pagination_schema import build_pagination
from app.schemas.user_schema import UserCreate, UserUpdate
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.settings_core_service import SettingsCoreService
from app.services.core.user_core_service import UserCoreService

logger = get_logger("luxswirl.web.services.web_users")


class UserRow:
    """Represents a single user row for UI display."""

    def __init__(
        self,
        user: User,
        session_count: int = 0,
    ):
        self.id = user.id
        self.username = user.username
        self.full_name = user.full_name
        self.role = user.role
        self.is_active = user.is_active
        self.locked_until = user.locked_until
        self.must_change_password = user.must_change_password
        self.failed_login_attempts = user.failed_login_attempts
        self.last_login_at = user.last_login_at
        self.password_changed_at = user.password_changed_at
        self.created_at = user.created_at
        self.session_count = session_count

    @property
    def is_locked(self) -> bool:
        """Check if account is currently locked."""
        if not self.locked_until:
            return False

        return self.locked_until > utc_now()

    @property
    def status_badge_class(self) -> str:
        """Get CSS class for status badge."""
        if self.is_locked:
            return "bg-red-600/20 text-red-400 border-red-600/30"
        if not self.is_active:
            return "bg-gray-600/20 text-gray-400 border-gray-600/30"
        return "bg-green-600/20 text-green-400 border-green-600/30"

    @property
    def role_badge_class(self) -> str:
        """Get CSS class for role badge."""
        if self.role == "admin":
            return "bg-purple-600/20 text-purple-400 border-purple-600/30"
        if self.role == "editor":
            return "bg-blue-600/20 text-blue-400 border-blue-600/30"
        return "bg-gray-600/20 text-gray-400 border-gray-600/30"


class WebUsersViewService:
    """Service for web UI user management."""

    def __init__(self):
        self.user_service = UserCoreService()
        self.auth_service = AuthCoreService()

    async def get_user_stats(self, db: AsyncSession) -> dict:
        """
        Get user statistics for web UI dashboard.

        Args:
            db: Database session

        Returns:
            Dict with user statistics
        """
        return cast(dict, await self.user_service.get_user_stats(db))

    async def list_users(
        self,
        db: AsyncSession,
        skip: int = 0,
        limit: int = 50,
        search: str | None = None,
        role: str | None = None,
        is_active: bool | None = None,
    ) -> tuple[list[UserRow], int]:
        """
        List users with pagination and filters.

        Args:
            db: Database session
            skip: Number of records to skip
            limit: Maximum number of records to return
            search: Search query (username, email, full_name)
            role: Filter by role
            is_active: Filter by active status

        Returns:
            Tuple of (user rows, total count)
        """
        users, total = await self.user_service.list_users(
            db,
            skip=skip,
            limit=limit,
            search=search,
            role=role,
            is_active=is_active,
        )

        # Convert to UserRow objects
        user_rows = [UserRow(user) for user in users]

        return user_rows, total

    async def get_user(self, db: AsyncSession, user_id: UUID) -> User | None:
        """
        Get user by ID.

        Args:
            db: Database session
            user_id: User ID

        Returns:
            User if found, None otherwise
        """
        return cast(User | None, await self.user_service.get_user_by_id(db, user_id))

    async def create_user(
        self,
        db: AsyncSession,
        *,
        username: str,
        password: str,
        role: str,
        full_name: str | None,
        is_active: bool,
        must_change_password: bool,
        created_by: str,
    ) -> tuple[User | None, str | None]:
        """Build the UserCreate DTO from raw form inputs and create the user.

        Returns (User, None) on success or (None, error_message) — including the
        first Pydantic validation message when the inputs are invalid.
        """
        try:
            user_data = UserCreate(
                username=username,
                password=password,
                role=UserRole(role),
                full_name=full_name or None,
                is_active=is_active,
                must_change_password=must_change_password,
            )
        except ValidationError as exc:
            errors = exc.errors()
            return None, (str(errors[0]["msg"]) if errors else "Invalid input")
        try:
            user = await self.user_service.create_user(db, user_data, created_by)
            return user, None
        except ValueError as e:
            return None, str(e)
        except Exception:
            logger.error("Error creating user", exc_info=True)
            return None, "An error occurred while creating user"

    async def update_user(
        self,
        db: AsyncSession,
        user_id: UUID,
        *,
        role: str,
        full_name: str | None,
        is_active: bool,
        must_change_password: bool,
    ) -> tuple[User | None, str | None]:
        """Build the UserUpdate DTO from raw form inputs and update the user."""
        update_data = UserUpdate(
            role=UserRole(role),
            full_name=full_name or None,
            is_active=is_active,
            must_change_password=must_change_password,
        )
        try:
            user = await self.user_service.update_user(db, user_id, update_data)
            if not user:
                return None, "User not found"
            return user, None
        except ValueError as e:
            return None, str(e)
        except Exception:
            logger.error("Error updating user", exc_info=True)
            return None, "An error occurred while updating user"

    async def delete_user(self, db: AsyncSession, user_id: UUID) -> tuple[bool, str | None]:
        """
        Delete user.

        Args:
            db: Database session
            user_id: User ID

        Returns:
            Tuple of (success, error message if failed)
        """
        try:
            success = await self.user_service.delete_user(db, user_id)
            if not success:
                return False, "User not found"
            return True, None
        except Exception:
            logger.error("Error deleting user", exc_info=True)
            return False, "An error occurred while deleting user"

    async def reset_password(
        self,
        db: AsyncSession,
        user_id: UUID,
        new_password: str,
        must_change: bool = True,
    ) -> tuple[bool, str | None]:
        """
        Reset user password (admin action).

        Args:
            db: Database session
            user_id: User ID
            new_password: New password
            must_change: Whether user must change password on next login

        Returns:
            Tuple of (success, error message if failed)
        """
        try:
            # Get the user first
            user = await self.user_service.get_user_by_id(db, user_id)
            if not user:
                return False, "User not found"

            # Reset password via AuthCoreService
            await self.auth_service.reset_password(db, user, new_password, must_change)
            return True, None
        except ValueError as e:
            return False, str(e)
        except Exception:
            logger.error("Error resetting password", exc_info=True)
            return False, "An error occurred while resetting password"

    async def unlock_user(self, db: AsyncSession, user_id: UUID) -> tuple[bool, str | None]:
        """
        Unlock user account.

        Args:
            db: Database session
            user_id: User ID

        Returns:
            Tuple of (success, error message if failed)
        """
        try:
            await self.user_service.unlock_user(db, user_id)
            return True, None
        except ValueError as e:
            return False, str(e)
        except Exception:
            logger.error("Error unlocking user", exc_info=True)
            return False, "An error occurred while unlocking user"

    async def ensure_default_admin(self, db: AsyncSession) -> User | None:
        """
        Ensure default admin user exists (seeded from env config, if set).

        Args:
            db: Database session

        Returns:
            Admin user, or None when first-run setup is deferred to the wizard.
        """
        return cast("User | None", await self.user_service.ensure_default_admin(db))

    async def needs_setup(self, db: AsyncSession) -> bool:
        """
        Whether the first-run setup wizard should be shown.

        True when no admin password is configured via env and no admin exists
        yet. Delegates to the core user service.

        Args:
            db: Database session

        Returns:
            True if the /setup wizard should handle first admin creation.
        """
        return cast(bool, await self.user_service.needs_setup(db))

    @staticmethod
    async def get_setting(db, key: str, default):
        return await SettingsCoreService.get_setting(db, key, default)

    async def build_users_page_context(
        self,
        db: AsyncSession,
        *,
        request,
        current_user,
        page: int,
        per_page: int | None,
        role: str | None,
        is_active: bool | None,
        search: str | None,
    ) -> dict:
        """Build the full template context for /settings/users."""
        if per_page is None:
            per_page = await SettingsCoreService.get_setting(db, "general.default_page_size", 50)

        skip = (page - 1) * per_page
        users, total = await self.list_users(
            db, skip=skip, limit=per_page, role=role, is_active=is_active, search=search
        )
        stats = await self.get_user_stats(db)
        filters = {"role": role, "is_active": is_active, "search": search}
        pagination = build_pagination(page=page, per_page=per_page, total=total, filters=filters)

        return {
            "request": request,
            "current_user": current_user,
            "users": users,
            "stats": stats,
            "pagination": pagination,
            # Filter dropdown uses short labels (just the role name title-cased)
            "role_options": [{"value": r.value, "label": r.value.title()} for r in UserRole],
            "filters": filters,
        }

    @staticmethod
    def build_user_create_form_context(*, current_user) -> dict:
        """Build context for the user create form panel.

        Form dropdown gets the longer descriptive labels from enum_model.
        """
        return {
            "current_user": current_user,
            "role_options": options_for(UserRole),
        }

    @staticmethod
    def build_user_edit_form_context(*, current_user, edit_user: User) -> dict:
        """Build context for the user edit form panel."""
        return {
            "current_user": current_user,
            "user": edit_user,
            "role_options": options_for(UserRole),
        }

    async def build_users_table_partial_context(
        self,
        db: AsyncSession,
        *,
        request,
        current_user,
        page: int,
        per_page: int | None,
        role: str | None,
        is_active: bool | None,
        search: str | None,
    ) -> dict:
        """Build context for the /settings/users/partials/table HTMX partial."""
        if per_page is None:
            per_page = await SettingsCoreService.get_setting(db, "general.default_page_size", 50)

        skip = (page - 1) * per_page
        users, total = await self.list_users(
            db, skip=skip, limit=per_page, role=role, is_active=is_active, search=search
        )
        filters = {"role": role, "is_active": is_active, "search": search}
        pagination = build_pagination(page=page, per_page=per_page, total=total, filters=filters)

        return {
            "request": request,
            "current_user": current_user,
            "users": users,
            "pagination": pagination,
        }
