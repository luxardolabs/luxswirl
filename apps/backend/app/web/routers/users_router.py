"""
Users router - web UI for user management (admin only).

Renders HTML pages and HTMX partials for managing users.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_deps import AdminUser
from app.db import get_db
from app.services.views.web_users_view_service import WebUsersViewService
from app.web.templates_config import templates

logger = get_logger("luxswirl.web.routers.users")

router = APIRouter(prefix="/settings", tags=["Web UI - Users"])


@router.get("/users", response_class=HTMLResponse, include_in_schema=False)
async def users_page(
    request: Request,
    user: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[
        int | None, Query(ge=10, le=200, description="Items per page (defaults to setting)")
    ] = None,
    role: Annotated[str | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
    search: Annotated[str | None, Query()] = None,
):
    """User management page (admin only)."""
    context = await WebUsersViewService().build_users_page_context(
        db,
        request=request,
        current_user=user,
        page=page,
        per_page=per_page,
        role=role,
        is_active=is_active,
        search=search,
    )
    return templates.TemplateResponse(request, "pages/settings/users.html", context)


@router.get("/users/partials/table", response_class=HTMLResponse, include_in_schema=False)
async def users_table_partial(
    request: Request,
    user: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[
        int | None, Query(ge=10, le=200, description="Items per page (defaults to setting)")
    ] = None,
    role: Annotated[str | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
    search: Annotated[str | None, Query()] = None,
):
    """HTMX partial: users table with filtering and pagination."""
    context = await WebUsersViewService().build_users_table_partial_context(
        db,
        request=request,
        current_user=user,
        page=page,
        per_page=per_page,
        role=role,
        is_active=is_active,
        search=search,
    )
    return templates.TemplateResponse(request, "partials/settings/users_table.html", context)


@router.get("/users/create-form", response_class=HTMLResponse, include_in_schema=False)
async def create_user_form(
    request: Request,
    user: AdminUser,
):
    """
    HTMX partial - returns form for creating a new user.
    """
    context = WebUsersViewService().build_user_create_form_context(current_user=user)
    return templates.TemplateResponse(
        request,
        "partials/settings/user_create_panel.html",
        context,
    )


@router.post("/users/create", response_class=HTMLResponse, include_in_schema=False)
async def create_user_submit(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    user: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    role: Annotated[str, Form()] = "viewer",
    full_name: Annotated[str | None, Form()] = None,
    is_active: Annotated[bool, Form()] = True,
    must_change_password: Annotated[bool, Form()] = True,
):
    """
    Handle create user form submission via HTMX.
    """
    web_users = WebUsersViewService()

    new_user, error = await web_users.create_user(
        db,
        username=username,
        password=password,
        role=role,
        full_name=full_name,
        is_active=is_active,
        must_change_password=must_change_password,
        created_by=user.username,
    )

    if error:
        return HTMLResponse(
            content=f"""
            <div class="card bg-red-500/10 border-red-500/20 p-4 mb-4">
                <div class="flex items-center gap-3">
                    <svg class="w-5 h-5 text-red-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>
                    </svg>
                    <span class="text-sm text-red-400 font-medium">{error}</span>
                </div>
            </div>
            """,
            status_code=400,
        )

    assert new_user is not None
    logger.info(
        "Admin created user via web UI",
        extra={
            "actor_username": user.username,
            "target_username": new_user.username,
            "target_user_id": str(new_user.id),
        },
    )

    # Return success and trigger page refresh + close panel
    return HTMLResponse(
        content="",
        status_code=200,
        headers={
            "HX-Trigger": "refreshPage",
            "HX-Refresh": "true",
        },
    )


@router.get("/users/{user_id}/edit-form", response_class=HTMLResponse, include_in_schema=False)
async def edit_user_form(
    request: Request,
    user_id: UUID,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    HTMX partial - returns form for editing a user.
    """

    web_users = WebUsersViewService()

    try:
        edit_user = await web_users.get_user(db, user_id)

        if not edit_user:
            return HTMLResponse(
                content="""
                <div class="card bg-red-500/10 border-red-500/20 p-4">
                    <div class="flex items-center gap-3">
                        <svg class="w-5 h-5 text-red-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>
                        </svg>
                        <span class="text-sm text-red-400 font-medium">User not found</span>
                    </div>
                </div>
                """,
                status_code=404,
            )

        context = WebUsersViewService().build_user_edit_form_context(
            current_user=admin, edit_user=edit_user
        )
        return templates.TemplateResponse(
            request,
            "partials/settings/user_edit_panel.html",
            context,
        )

    except ValueError:
        return HTMLResponse(
            content="""
            <div class="card bg-red-500/10 border-red-500/20 p-4">
                <div class="flex items-center gap-3">
                    <svg class="w-5 h-5 text-red-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>
                    </svg>
                    <span class="text-sm text-red-400 font-medium">Invalid user ID</span>
                </div>
            </div>
            """,
            status_code=400,
        )


@router.post("/users/{user_id}/update", response_class=HTMLResponse, include_in_schema=False)
async def update_user_submit(
    request: Request,
    user_id: UUID,
    role: Annotated[str, Form()],
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    full_name: Annotated[str | None, Form()] = None,
    is_active: Annotated[bool, Form()] = False,
    must_change_password: Annotated[bool, Form()] = False,
):
    """
    Handle edit user form submission via HTMX.
    """

    web_users = WebUsersViewService()

    updated_user, error = await web_users.update_user(
        db,
        user_id,
        role=role,
        full_name=full_name,
        is_active=is_active,
        must_change_password=must_change_password,
    )

    if error:
        return HTMLResponse(
            content=f"""
            <div class="card bg-red-500/10 border-red-500/20 p-4 mb-4">
                <div class="flex items-center gap-3">
                    <svg class="w-5 h-5 text-red-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>
                    </svg>
                    <span class="text-sm text-red-400 font-medium">{error}</span>
                </div>
            </div>
            """,
            status_code=400,
        )

    assert updated_user is not None
    logger.info(
        "Admin updated user via web UI",
        extra={
            "actor_username": admin.username,
            "target_username": updated_user.username,
            "target_user_id": str(updated_user.id),
        },
    )

    # Return success and trigger page refresh + close panel
    return HTMLResponse(
        content="",
        status_code=200,
        headers={
            "HX-Trigger": "refreshPage",
            "HX-Refresh": "true",
        },
    )


@router.post("/users/{user_id}/delete", include_in_schema=False)
async def delete_user(
    user_id: UUID,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Delete user (admin only).
    """

    web_users = WebUsersViewService()

    # Prevent deleting self
    if admin.id == user_id:
        return RedirectResponse(
            url="/settings/users?error=Cannot delete your own account",
            status_code=302,
        )

    success, error = await web_users.delete_user(db, user_id)

    if error:
        return RedirectResponse(
            url=f"/settings/users?error={error}",
            status_code=302,
        )

    logger.warning(
        "Admin deleted user via web UI",
        extra={
            "actor_username": admin.username,
            "target_user_id": str(user_id),
        },
    )

    return RedirectResponse(
        url="/settings/users?success=User deleted successfully",
        status_code=302,
    )


@router.post("/users/{user_id}/reset-password", include_in_schema=False)
async def reset_user_password(
    request: Request,
    user_id: UUID,
    new_password: Annotated[str, Form()],
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    must_change: Annotated[bool, Form()] = True,
):
    """
    Reset user password (admin only).
    """

    web_users = WebUsersViewService()

    target_user = await web_users.get_user(db, user_id)

    if not target_user:
        return RedirectResponse(url="/settings/users?error=User not found", status_code=302)

    success, error = await web_users.reset_password(db, user_id, new_password, must_change)

    if error:
        return RedirectResponse(
            url=f"/settings/users?error={error}",
            status_code=302,
        )

    logger.warning(
        "Admin reset password for user via web UI",
        extra={
            "actor_username": admin.username,
            "target_username": target_user.username,
            "target_user_id": str(target_user.id),
        },
    )

    return RedirectResponse(
        url=f"/settings/users?success=Password reset for {target_user.username}",
        status_code=302,
    )


@router.post("/users/{user_id}/unlock", include_in_schema=False)
async def unlock_user(
    user_id: UUID,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Unlock locked user account (admin only).
    """

    web_users = WebUsersViewService()

    success, error = await web_users.unlock_user(db, user_id)

    if error:
        return RedirectResponse(
            url=f"/settings/users?error={error}",
            status_code=302,
        )

    unlocked_user = await web_users.get_user(db, user_id)
    assert unlocked_user is not None

    logger.info(
        "Admin unlocked user via web UI",
        extra={
            "actor_username": admin.username,
            "target_username": unlocked_user.username,
            "target_user_id": str(unlocked_user.id),
        },
    )

    return RedirectResponse(
        url=f"/settings/users?success=User {unlocked_user.username} unlocked",
        status_code=302,
    )
