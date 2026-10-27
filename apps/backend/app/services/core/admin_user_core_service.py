"""
Admin user core service - user accounts as the admin API serves them.

An admin's alone, as in the web UI: every change re-reads that the acting
user is an active admin (`UserCoreService.require_active_admin`), whoever calls
it. The rules are `UserCoreService`'s and
`AuthCoreService`'s, whichever path asks: the password policy, the last active
admin, no deleting your own account, and a reset ending the user's sessions
and API tokens.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import UserNotFoundException
from app.models.enum_model import UserRole
from app.schemas.admin_user_schema import (
    AdminPasswordReset,
    AdminUserCreate,
    AdminUserUpdate,
    UserPage,
)
from app.schemas.pagination_schema import build_pagination
from app.schemas.user_schema import UserResponse
from app.services.core.auth_core_service import AuthCoreService
from app.services.core.user_core_service import UserCoreService


class AdminUserCoreService:
    """List, read, create, change and delete users; reset and unlock them."""

    @staticmethod
    async def list_users(
        db: AsyncSession,
        *,
        page: int,
        per_page: int,
        role: UserRole | None,
        is_active: bool | None,
        search: str | None,
    ) -> UserPage:
        page = max(1, page)
        users, total = await UserCoreService().list_users(
            db,
            skip=(page - 1) * per_page,
            limit=per_page,
            role=role,
            is_active=is_active,
            search=search,
        )
        return UserPage.of(
            users, build_pagination(page=page, per_page=per_page, total=total)
        )

    @staticmethod
    async def get_user(db: AsyncSession, user_id: UUID) -> UserResponse:
        user = await UserCoreService().get_user(db, user_id)
        if user is None:
            raise UserNotFoundException(user_id)
        return user

    @staticmethod
    async def create_user(
        db: AsyncSession, actor_id: UUID, actor_name: str, data: AdminUserCreate
    ) -> UserResponse:
        """A new user, recorded as created by the acting admin."""
        await UserCoreService.require_active_admin(db, actor_id)
        return await UserCoreService().create_user(db, data, created_by=actor_name)

    @staticmethod
    async def update_user(
        db: AsyncSession, actor_id: UUID, user_id: UUID, data: AdminUserUpdate
    ) -> UserResponse:
        await UserCoreService.require_active_admin(db, actor_id)
        return await UserCoreService().update_user(db, user_id, data)

    @staticmethod
    async def delete_user(db: AsyncSession, actor_id: UUID, user_id: UUID) -> None:
        await UserCoreService.require_active_admin(db, actor_id)
        await UserCoreService().delete_user(db, user_id, actor_id=actor_id)

    @staticmethod
    async def reset_password(
        db: AsyncSession, actor_id: UUID, user_id: UUID, data: AdminPasswordReset
    ) -> UserResponse:
        """Set the user's password; their sessions and API tokens end."""
        await UserCoreService.require_active_admin(db, actor_id)
        await AuthCoreService().reset_password(
            db, user_id, data.new_password, data.must_change
        )
        return await AdminUserCoreService.get_user(db, user_id)

    @staticmethod
    async def unlock_user(
        db: AsyncSession, actor_id: UUID, user_id: UUID
    ) -> UserResponse:
        """Clear the user's failed sign-ins and lock."""
        await UserCoreService.require_active_admin(db, actor_id)
        return await UserCoreService().unlock_user(db, user_id)
