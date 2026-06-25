"""
Status Pages router — web UI for managing custom status pages/dashboards.
"""

import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Path, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_deps import (
    CurrentUserWeb,
    EditorUserWeb,
    OptionalUserWeb,
)
from app.core.exceptions import StatusPageNotFoundException
from app.core.query_params import AgentIdFilter, CheckTypeFilter, HealthStatusFilter
from app.db import get_db
from app.services.views.status_pages_view_service import StatusPagesViewService
from app.web._hx_responses import hx_empty_with_toast, hx_toast_trigger
from app.web.routers._render import error_page, error_partial, public_error_page
from app.web.templates_config import templates

logger = get_logger("luxswirl.web.routers.status_pages")

router = APIRouter(tags=["Web UI - Status Pages"])


# ---- list / forms ---------------------------------------------------------


@router.get("/status-pages", response_class=HTMLResponse, include_in_schema=False)
async def status_pages_list(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUserWeb,
    is_public: Annotated[str, Query(description="Filter by public/private status")] = "all",
    page: Annotated[int, Query(ge=1, description="Page number")] = 1,
    per_page: Annotated[
        int | None, Query(ge=10, le=200, description="Items per page (defaults to setting)")
    ] = None,
):
    """Status pages list page — manage custom status dashboards."""
    try:
        context = await StatusPagesViewService.build_list_context(
            db, request, current_user, is_public, page, per_page
        )
        return templates.TemplateResponse(request, "pages/status_pages.html", context)
    except Exception as e:
        logger.error("Error rendering status pages list", exc_info=True)
        return error_page(request, current_user, str(e), 500)


@router.get("/status-pages/create-form", response_class=HTMLResponse, include_in_schema=False)
async def create_form(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUserWeb,
):
    """Get the create form for a new status page."""
    return templates.TemplateResponse(
        request,
        "partials/status_page_form.html",
        StatusPagesViewService.build_create_form_context(request, current_user),
    )


@router.get(
    "/status-pages/{status_page_id}/edit-form",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def edit_form(
    request: Request,
    status_page_id: Annotated[UUID, Path(description="Status page UUID")],
    current_user: CurrentUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get the edit form for an existing status page."""
    try:
        context = await StatusPagesViewService.build_edit_form_context(
            db, request, current_user, status_page_id
        )
        return templates.TemplateResponse(request, "partials/status_page_form.html", context)
    except StatusPageNotFoundException as e:
        logger.error("Status page not found", exc_info=True)
        return error_partial(request, current_user, str(e), 404)


# ---- create / update / delete --------------------------------------------


@router.post("/status-pages/create", response_class=HTMLResponse, include_in_schema=False)
async def create_status_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
    name: Annotated[str, Form()],
    slug: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    is_public: Annotated[bool, Form()] = False,
    status_bar_minutes: Annotated[int, Form()] = 30,
):
    """Create a new status page."""
    await StatusPagesViewService.create_status_page(
        db, name, slug, description, is_public, status_bar_minutes
    )
    return HTMLResponse(
        content="",
        status_code=200,
        headers={"HX-Trigger": "closeSidePanel,refreshPage"},
    )


@router.post(
    "/status-pages/{status_page_id}/update",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def update_status_page(
    request: Request,
    status_page_id: Annotated[UUID, Path(description="Status page UUID")],
    current_user: EditorUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
    name: Annotated[str, Form()],
    slug: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    is_public: Annotated[bool, Form()] = False,
    status_bar_minutes: Annotated[int, Form()] = 30,
):
    """Update an existing status page."""
    await StatusPagesViewService.update_status_page(
        db, status_page_id, name, slug, description, is_public, status_bar_minutes
    )
    return HTMLResponse(
        content="",
        status_code=200,
        headers={"HX-Trigger": "closeSidePanel,refreshPage"},
    )


@router.delete(
    "/status-pages/{status_page_id}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def delete_status_page(
    request: Request,
    status_page_id: Annotated[UUID, Path(description="Status page UUID")],
    current_user: EditorUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Enqueue a status_page_delete maintenance job; return the polling partial.

    Status pages cascade through dashboard_items. The cascade runs in the
    maintenance worker on its own session so this request commits in <100ms.
    See LUXSWIRL-105.
    """
    job = await StatusPagesViewService.enqueue_delete(db, status_page_id, owner_id=current_user.id)
    return templates.TemplateResponse(
        request,
        "partials/maintenance/job_status.html",
        {"job": job, "request": request, "current_user": current_user},
    )


# ---- manage page + dashboard ops -----------------------------------------


@router.get(
    "/status-pages/{status_page_id}/manage",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def manage_status_page(
    request: Request,
    status_page_id: Annotated[UUID, Path(description="Status page UUID")],
    current_user: CurrentUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Management page for editing status page items (checks and groups)."""
    try:
        context = await StatusPagesViewService.build_manage_context(
            db, request, current_user, status_page_id
        )
        return templates.TemplateResponse(request, "pages/status_page_manage.html", context)
    except StatusPageNotFoundException as e:
        logger.error("Status page not found", exc_info=True)
        return error_page(request, current_user, str(e), 404)
    except Exception as e:
        logger.error("Error rendering manage page", exc_info=True)
        return error_page(request, current_user, str(e), 500)


@router.get(
    "/status-pages/{status_page_id}/available-checks",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def get_available_checks(
    request: Request,
    status_page_id: Annotated[UUID, Path(description="Status page UUID")],
    current_user: CurrentUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
    agent_id: AgentIdFilter = None,
    check_type: CheckTypeFilter = None,
    status: HealthStatusFilter = None,
    tags: Annotated[str, Query(description="Comma-separated tags")] = "",
    search: Annotated[str, Query(description="Search query")] = "",
):
    """Get filtered list of available checks for adding to dashboard."""
    try:
        context = await StatusPagesViewService.build_available_checks_context(
            db,
            request,
            current_user,
            status_page_id,
            agent_id,
            check_type,
            status,
            tags,
            search,
        )
        return templates.TemplateResponse(request, "partials/available_checks.html", context)
    except Exception:
        logger.error("Error getting available checks", exc_info=True)
        return HTMLResponse(content="", status_code=500)


@router.post(
    "/status-pages/{status_page_id}/add-check",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def add_check_to_dashboard(
    request: Request,
    status_page_id: Annotated[UUID, Path(description="Status page UUID")],
    check_id: Annotated[UUID, Form()],
    current_user: EditorUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Add a check to the dashboard. Returns refreshed dashboard_items partial."""
    try:
        context, error = await StatusPagesViewService.add_check_to_dashboard(
            db, request, current_user, status_page_id, check_id
        )
        if error == "no_items":
            return HTMLResponse(content="", status_code=500)
        return templates.TemplateResponse(
            request,
            "partials/dashboard_items.html",
            context,
            headers={"HX-Trigger": hx_toast_trigger("Check added to dashboard")},
        )
    except Exception:
        logger.error("Error adding check", exc_info=True)
        return HTMLResponse(content="", status_code=500)


@router.delete(
    "/status-pages/{status_page_id}/remove-item/{item_index}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def remove_item_from_dashboard(
    request: Request,
    status_page_id: Annotated[UUID, Path(description="Status page UUID")],
    item_index: Annotated[int, Path(description="Item index")],
    current_user: EditorUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Remove an item from the dashboard."""
    try:
        context = await StatusPagesViewService.remove_item_from_dashboard(
            db, request, current_user, status_page_id, item_index
        )
        return templates.TemplateResponse(
            request,
            "partials/dashboard_items.html",
            context,
            headers={"HX-Trigger": hx_toast_trigger("Item removed from dashboard")},
        )
    except Exception:
        logger.error("Error removing item", exc_info=True)
        return hx_empty_with_toast("Failed to remove item", kind="error", status_code=500)


@router.post(
    "/status-pages/{status_page_id}/reorder",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def reorder_dashboard_items(
    request: Request,
    status_page_id: Annotated[UUID, Path(description="Status page UUID")],
    items_json: Annotated[str, Form()],
    current_user: EditorUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Reorder items in the dashboard. Returns refreshed dashboard_items partial."""
    items = json.loads(items_json)
    context = await StatusPagesViewService.reorder_dashboard_items(
        db, request, current_user, status_page_id, items
    )
    return templates.TemplateResponse(request, "partials/dashboard_items.html", context)


@router.post(
    "/status-pages/{status_page_id}/add-group",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def add_group_to_dashboard(
    request: Request,
    status_page_id: Annotated[UUID, Path(description="Status page UUID")],
    name: Annotated[str, Form()],
    current_user: EditorUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
    filter_json: Annotated[str | None, Form()] = None,
):
    """Add a group to the dashboard. Returns refreshed dashboard_items partial."""
    body: dict = {"name": name}
    if filter_json is not None:
        body["filter"] = json.loads(filter_json)
    context, error = await StatusPagesViewService.add_group_to_dashboard(
        db, request, current_user, status_page_id, body
    )
    if error == "no_items":
        return HTMLResponse(content="", status_code=500)
    return templates.TemplateResponse(
        request,
        "partials/dashboard_items.html",
        context,
        headers={"HX-Trigger": hx_toast_trigger("Group added")},
    )


@router.patch(
    "/status-pages/{status_page_id}/rename-group/{item_index}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def rename_group_in_dashboard(
    request: Request,
    status_page_id: Annotated[UUID, Path(description="Status page UUID")],
    item_index: Annotated[int, Path(description="Item index")],
    name: Annotated[str, Form()],
    current_user: EditorUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Rename a group. Returns refreshed dashboard_items partial."""
    if not name.strip():
        return HTMLResponse(content="", status_code=400)
    context = await StatusPagesViewService.rename_group(
        db, request, current_user, status_page_id, item_index, name.strip()
    )
    return templates.TemplateResponse(request, "partials/dashboard_items.html", context)


@router.patch(
    "/status-pages/{status_page_id}/update-group-filters/{item_index}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def update_group_filters_in_dashboard(
    request: Request,
    status_page_id: Annotated[UUID, Path(description="Status page UUID")],
    item_index: Annotated[int, Path(description="Item index")],
    filter_json: Annotated[str, Form()],
    current_user: EditorUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Update filters for a dynamic filter group. Returns dashboard_items partial."""
    filter = json.loads(filter_json) if filter_json else {}
    context = await StatusPagesViewService.update_group_filters(
        db, request, current_user, status_page_id, item_index, filter
    )
    return templates.TemplateResponse(request, "partials/dashboard_items.html", context)


@router.patch(
    "/status-pages/{status_page_id}/group/{item_index}/sort",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def update_group_sort_in_dashboard(
    request: Request,
    status_page_id: Annotated[UUID, Path(description="Status page UUID")],
    item_index: Annotated[int, Path(description="Item index")],
    current_user: EditorUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
    sort_by: Annotated[str, Form()] = "manual",
    sort_direction: Annotated[str, Form()] = "asc",
):
    """Update sort settings for a group. Returns refreshed dashboard_items partial."""
    context = await StatusPagesViewService.update_group_sort(
        db, request, current_user, status_page_id, item_index, sort_by, sort_direction
    )
    return templates.TemplateResponse(request, "partials/dashboard_items.html", context)


@router.get(
    "/status-pages/{status_page_id}/dashboard-items",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def get_dashboard_items(
    request: Request,
    status_page_id: Annotated[UUID, Path(description="Status page UUID")],
    current_user: CurrentUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get dashboard items HTML (used for refreshing after filter changes)."""
    try:
        context = await StatusPagesViewService.build_dashboard_items_context(
            db, request, current_user, status_page_id
        )
        return templates.TemplateResponse(request, "partials/dashboard_items.html", context)
    except Exception:
        logger.error("Error getting dashboard items", exc_info=True)
        return HTMLResponse(content="", status_code=500)


# ---- public view ----------------------------------------------------------


@router.get("/status/{slug}", response_class=HTMLResponse, include_in_schema=False)
async def view_public_status_page(
    request: Request,
    slug: Annotated[str, Path(description="Status page slug")],
    current_user: OptionalUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
    time_range: Annotated[int | None, Query(description="Status bar time range in minutes")] = None,
):
    """Public view of a status page by slug at /status/{slug}."""
    try:
        context, signal = await StatusPagesViewService.build_public_view(
            db, request, current_user, slug, time_range
        )
        if signal == "redirect_login":
            return RedirectResponse(url=f"/login?redirect=/status/{slug}", status_code=307)
        if signal == "not_found":
            return public_error_page(
                request, "This status page does not exist.", 404, title="Status page not found"
            )
        return templates.TemplateResponse(request, "pages/status_page_public.html", context)
    except StatusPageNotFoundException:
        logger.warning("Status page not found", extra={"slug": slug})
        return public_error_page(
            request, "This status page does not exist.", 404, title="Status page not found"
        )
    except Exception:
        logger.error("Error rendering public status page", exc_info=True)
        return public_error_page(request, "Something went wrong loading this status page.", 500)


@router.get("/status/{slug}/partial", response_class=HTMLResponse, include_in_schema=False)
async def view_public_status_page_partial(
    request: Request,
    slug: Annotated[str, Path(description="Status page slug")],
    current_user: OptionalUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
    time_range: Annotated[int | None, Query(description="Status bar time range in minutes")] = None,
):
    """Partial refresh endpoint for public status page (HTMX polling)."""
    try:
        context, status_code = await StatusPagesViewService.build_public_view_partial(
            db, request, current_user, slug, time_range
        )
        if status_code is not None:
            return HTMLResponse(content="", status_code=status_code)
        return templates.TemplateResponse(
            request, "partials/status_page_public_content.html", context
        )
    except Exception:
        logger.error("Error rendering public status page partial", exc_info=True)
        return HTMLResponse(content="", status_code=500)
