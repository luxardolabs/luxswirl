"""
Web users service - handles user management for web UI.

This service provides user management functionality specifically for web UI,
calling core user service and formatting responses for templates.
"""

from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import Request
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_utils import utc_now
from app.core.exceptions import (
    LuxSwirlException,
    validation_message,
    value_error_message,
)
from app.models.enum_model import UserRole, options_for
from app.schemas.auth_schema import AuthedUser
from app.schemas.pagination_schema import build_pagination
from app.schemas.user_schema import (
    UserCreate,
    UserResponse,
    UserUpdate,
    UserUrls,
)
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.settings_core_service import SettingsCoreService
from app.services.core.user_core_service import UserCoreService
from app.shared.logger import get_logger

logger = get_logger("luxswirl.web.services.web_users")


def build_user_urls(request: Request, user_id: UUID) -> UserUrls:
    # .path: url_for returns an absolute url from the request's scheme/host, which
    # behind TLS-terminating nginx can emit http:// into an https:// page.
    def u(name: str) -> str:
        return str(request.url_for(name, user_id=user_id).path)

    return UserUrls(
        edit_form=u("users.edit"),
        update=u("users.update"),
        unlock=u("users.unlock"),
        delete=u("users.delete"),
        reset_password=u("users.reset_password"),
    )


class UserRow:
    """Represents a single user row for UI display."""

    def __init__(
        self,
        user: UserResponse,
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
        self.urls: UserUrls | None = None

    @property
    def is_locked(self) -> bool:
        """Check if account is currently locked."""
        if not self.locked_until:
            return False

        return self.locked_until > utc_now()


class WebUsersViewService:
    """Service for web UI user management."""

    def __init__(self) -> None:
        self.user_service = UserCoreService()
        self.auth_service = AuthCoreService()

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
    ) -> tuple[UserResponse | None, str | None]:
        """Build the UserCreate DTO from raw form inputs and create the user.

        Returns (User, None) on success or (None, error_message), naming each
        refused field when the inputs are invalid.
        """
        try:
            # The schema reads the role, so an unknown one is refused by field.
            user_data = UserCreate.model_validate(
                {
                    "username": username,
                    "password": password,
                    "role": role,
                    "full_name": full_name or None,
                    "is_active": is_active,
                    "must_change_password": must_change_password,
                }
            )
        except ValidationError as exc:
            return None, validation_message(exc)
        try:
            user = await self.user_service.create_user(db, user_data, created_by)
            return user, None
        except ValueError as e:
            return None, value_error_message(e)
        except LuxSwirlException as e:
            return None, e.message

    async def update_user(
        self,
        db: AsyncSession,
        user_id: UUID,
        *,
        role: str,
        full_name: str | None,
        is_active: bool,
        must_change_password: bool,
    ) -> tuple[UserResponse | None, str | None]:
        """Build the UserUpdate DTO from raw form inputs and update the user.

        A refused input raises the schema's ValidationError, which the app's 400
        handler renders as field and reason.
        """
        update_data = UserUpdate.model_validate(
            {
                "role": role,
                "full_name": full_name or None,
                "is_active": is_active,
                "must_change_password": must_change_password,
            }
        )
        try:
            user = await self.user_service.update_user(db, user_id, update_data)
            if not user:
                return None, "User not found"
            return user, None
        except ValueError as e:
            return None, value_error_message(e)
        except LuxSwirlException as e:
            return None, e.message

    async def delete_user(
        self, db: AsyncSession, user_id: UUID, actor_id: UUID
    ) -> tuple[bool, str | None]:
        """
        Delete user.

        Args:
            db: Database session
            user_id: User ID
            actor_id: The admin asking (who cannot delete themselves)

        Returns:
            Tuple of (success, error message if failed)
        """
        try:
            await self.user_service.delete_user(db, user_id, actor_id=actor_id)
        except LuxSwirlException as e:
            return False, e.message
        return True, None

    async def reset_password(
        self,
        db: AsyncSession,
        user_id: UUID,
        new_password: str,
        must_change: bool = True,
    ) -> tuple[UserResponse | None, str | None]:
        """
        Reset user password (admin action).

        Args:
            db: Database session
            user_id: User ID
            new_password: New password
            must_change: Whether user must change password on next login

        Returns:
            Tuple of (the user whose password was reset, error message if
            failed). The caller names the user in its audit line and its
            confirmation, so hand it back rather than making it re-read.
        """
        try:
            user = await self.user_service.get_user(db, user_id)
            if not user:
                return None, "User not found"

            await self.auth_service.reset_password(
                db, user.id, new_password, must_change
            )
            return user, None
        except ValueError as e:
            return None, value_error_message(e)
        except LuxSwirlException as e:
            return None, e.message

    async def unlock_user(
        self, db: AsyncSession, user_id: UUID
    ) -> tuple[UserResponse | None, str | None]:
        """
        Unlock user account.

        Args:
            db: Database session
            user_id: User ID

        Returns:
            Tuple of (the unlocked user, error message if failed). The caller
            names the user in its confirmation, so hand it back rather than
            making it re-read the row that was just written.
        """
        try:
            return await self.user_service.unlock_user(db, user_id), None
        except ValueError as e:
            return None, value_error_message(e)
        except LuxSwirlException as e:
            return None, e.message

    async def build_users_page_context(
        self,
        db: AsyncSession,
        *,
        request: Request,
        current_user: AuthedUser,
        page: int,
        per_page: int | None,
        role: str | None,
        is_active: bool | None,
        search: str | None,
    ) -> dict[str, Any]:
        """Build the full template context for /settings/users."""
        if per_page is None:
            per_page = await SettingsCoreService.get_setting(
                db, "general.default_page_size", 50
            )

        skip = (page - 1) * per_page
        users, total = await self.list_users(
            db, skip=skip, limit=per_page, role=role, is_active=is_active, search=search
        )
        for row in users:
            row.urls = build_user_urls(request, row.id)
        stats = await self.user_service.get_user_stats(db)
        filters = {"role": role, "is_active": is_active, "search": search}
        pagination = build_pagination(
            page=page, per_page=per_page, total=total, filters=filters
        )

        return {
            "request": request,
            "current_user": current_user,
            "users": users,
            "stats": stats,
            "pagination": pagination,
            # Filter dropdown uses short labels (just the role name title-cased)
            "role_options": [
                {"value": r.value, "label": r.value.title()} for r in UserRole
            ],
            "filters": filters,
            "page_title": "Settings - Users",
            # The page's own path: filters and pagination re-request it.
            "list_url": str(request.app.url_path_for("users.index")),
            "create_form_url": str(request.app.url_path_for("users.new")),
        }

    @staticmethod
    def build_user_create_form_context(
        *, request: Request, current_user: AuthedUser
    ) -> dict[str, Any]:
        """Build context for the user create form panel.

        Form dropdown gets the longer descriptive labels from enum_model.
        """
        return {
            "current_user": current_user,
            "role_options": options_for(UserRole),
            "create_url": str(request.app.url_path_for("users.create")),
        }

    @staticmethod
    def users_page_url(request: Request, **params: str) -> str:
        """The users page, resolved by route name, with `params` as its query
        (the page shows `error` / `success` as a banner)."""
        path = str(request.app.url_path_for("users.index"))
        return f"{path}?{urlencode(params)}" if params else path

    @staticmethod
    def build_user_edit_form_context(
        *, request: Request, current_user: AuthedUser, edit_user: UserResponse
    ) -> dict[str, Any]:
        """Build context for the user edit form panel."""
        return {
            "user_urls": build_user_urls(request, edit_user.id),
            "current_user": current_user,
            "user": edit_user,
            "role_options": options_for(UserRole),
        }

    async def build_user_edit_panel_context(
        self,
        db: AsyncSession,
        *,
        request: Request,
        current_user: AuthedUser,
        user_id: UUID,
    ) -> dict[str, Any] | None:
        """Read the user and assemble the edit panel, or None if it is gone.

        Reading and assembling belong together: the router's job is to turn the
        None into its 404, not to fetch the row itself.
        """
        edit_user = await self.user_service.get_user(db, user_id)
        if not edit_user:
            return None
        return WebUsersViewService.build_user_edit_form_context(
            request=request, current_user=current_user, edit_user=edit_user
        )

    async def build_users_table_partial_context(
        self,
        db: AsyncSession,
        *,
        request: Request,
        current_user: AuthedUser,
        page: int,
        per_page: int | None,
        role: str | None,
        is_active: bool | None,
        search: str | None,
    ) -> dict[str, Any]:
        """Build context for the /settings/users/partials/table HTMX partial."""
        if per_page is None:
            per_page = await SettingsCoreService.get_setting(
                db, "general.default_page_size", 50
            )

        skip = (page - 1) * per_page
        users, total = await self.list_users(
            db, skip=skip, limit=per_page, role=role, is_active=is_active, search=search
        )
        for row in users:
            row.urls = build_user_urls(request, row.id)
        filters = {"role": role, "is_active": is_active, "search": search}
        pagination = build_pagination(
            page=page, per_page=per_page, total=total, filters=filters
        )

        return {
            "request": request,
            "current_user": current_user,
            "users": users,
            "pagination": pagination,
            "list_url": str(request.app.url_path_for("users.index")),
        }
