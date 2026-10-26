"""
Checks router — web UI for managing health checks.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Path, Query, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import CheckNotFoundException
from app.core.query_params import CheckTypeFilter
from app.db import get_db
from app.services.views.checks_view_service import ChecksViewService
from app.services.views.status_view_service import StatusViewService
from app.shared.logger import get_logger
from app.web.check_form import (
    CheckCloneFields,
    CheckCreateFields,
    CheckUpdateFields,
    OptionalIdForm,
)
from app.web.deps import EditorUserWeb
from app.web.hx_responses import hx_empty_with_toast
from app.web.render import bulk_oob_response, error_partial, job_status_oob_response
from app.web.templates import templates

logger = get_logger("luxswirl.web.routers.checks")

router = APIRouter(tags=["Web UI - Checks"])

# ---- HTMX selectors / forms ---------------------------------------------


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.get(
    "/checks/assignment-mode-selector",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="assignment_mode_selector",
)
async def assignment_mode_selector(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: EditorUserWeb,
    assignment_mode: Annotated[str, Query()] = "manual",
) -> HTMLResponse:
    """Return assignment selector UI based on mode."""
    context = await ChecksViewService.build_assignment_mode_selector_context(
        db, request, current_user, assignment_mode
    )
    if context is None:
        return HTMLResponse(content="")
    return templates.TemplateResponse(
        request, "partials/check_assignment_selector.html", context
    )


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.get(
    "/checks/create-form",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="check_create_form",
)
async def create_form(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: EditorUserWeb,
) -> HTMLResponse:
    """Get the create form for a new check."""
    context = await ChecksViewService.build_create_form_context(
        db, request, current_user
    )
    return templates.TemplateResponse(
        request, "partials/panels/checks/check_form_panel.html", context
    )


@router.get(
    "/checks/{check_id}/edit-form",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="check_edit_form",
)
async def edit_form(
    request: Request,
    check_id: Annotated[UUID, Path(description="Check UUID")],
    current_user: EditorUserWeb,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> Response:
    """Get the edit form for an existing check."""
    try:
        context = await ChecksViewService.build_edit_form_context(
            db, request, current_user, check_id
        )
        return templates.TemplateResponse(
            request, "partials/panels/checks/check_form_panel.html", context
        )
    except CheckNotFoundException as e:
        logger.exception("Check not found")
        return error_partial(request, current_user, str(e), 404)


@router.get(
    "/checks/{check_id}/clone-form",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="clone_form",
)
async def clone_form(
    request: Request,
    check_id: Annotated[UUID, Path(description="Check UUID to clone")],
    current_user: EditorUserWeb,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> Response:
    """Get the clone form for an existing check."""
    try:
        context = await ChecksViewService.build_clone_form_context(
            db, request, current_user, check_id
        )
        return templates.TemplateResponse(
            request, "partials/panels/checks/check_form_panel.html", context
        )
    except CheckNotFoundException as e:
        logger.exception("Check not found")
        return error_partial(request, current_user, str(e), 404)


# ---- Mutations ----------------------------------------------------------


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.post(
    "/checks/{check_id}/clone",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="clone_check_handler",
)
async def clone_check_handler(
    request: Request,
    check_id: Annotated[UUID, Path(description="Check UUID to clone")],
    current_user: EditorUserWeb,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    fields: Annotated[CheckCloneFields, Depends()],
) -> Response:
    """Create a clone of an existing check."""
    return await ChecksViewService.handle_clone_check_form(
        db, request, check_id, current_user, fields.as_dict()
    )


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.post(
    "/checks/create",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="create_check_assignment",
)
async def create_check_assignment(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: EditorUserWeb,
    fields: Annotated[CheckCreateFields, Depends()],
) -> Response:
    """Create a new check (or bulk import if check_type is http-bulk)."""
    return await ChecksViewService.handle_create_check_form(
        db, request, current_user, fields.as_dict()
    )


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.post(
    "/checks/{check_id}/update",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="web_update_check",
)
async def update_check(
    request: Request,
    check_id: Annotated[UUID, Path(description="Check UUID")],
    current_user: EditorUserWeb,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    fields: Annotated[CheckUpdateFields, Depends()],
) -> Response:
    """Update an existing check."""
    return await ChecksViewService.handle_update_check_form(
        db, request, check_id, current_user, fields.as_dict()
    )


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.post(
    "/checks/{check_id}/toggle",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="toggle_check",
)
async def toggle_check(
    request: Request,
    check_id: Annotated[UUID, Path(description="Check UUID")],
    current_user: EditorUserWeb,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> HTMLResponse:
    """Toggle check enabled/disabled status."""
    context = await ChecksViewService.toggle_check_handler(db, request, check_id)
    return templates.TemplateResponse(
        request, "partials/checks/toggle_button.html", context
    )


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.delete(
    "/checks/{check_id}",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="web_delete_check",
)
async def delete_check(
    request: Request,
    check_id: Annotated[UUID, Path(description="Check UUID")],
    current_user: EditorUserWeb,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> Response:
    """Delete a check: enqueues a maintenance job that runs the cascade."""
    return await ChecksViewService.enqueue_check_delete(
        db, request, current_user, check_id
    )


# ---- Dependents management ----------------------------------------------


@router.get(
    "/checks/{check_id}/dependents",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="dependents_panel",
)
async def dependents_panel(
    request: Request,
    check_id: Annotated[UUID, Path(description="Parent check UUID")],
    current_user: EditorUserWeb,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    check_type: CheckTypeFilter = None,
    agent_name: Annotated[str, Query()] = "",
    tags: Annotated[str, Query()] = "",
    search: Annotated[str, Query()] = "",
) -> Response:
    """Side-panel: manage which checks depend on this one."""
    try:
        context = await ChecksViewService.build_dependents_panel_context(
            db,
            request,
            current_user,
            check_id,
            check_type=check_type,
            agent_name=agent_name,
            tags=tags,
            search=search,
        )
        return templates.TemplateResponse(
            request, "partials/panels/checks/dependents_panel.html", context
        )
    except CheckNotFoundException as e:
        return error_partial(request, current_user, str(e), 404)


@router.post(
    "/checks/{check_id}/dependents",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="update_dependents",
)
async def update_dependents(
    request: Request,
    check_id: Annotated[UUID, Path(description="Parent check UUID")],
    current_user: EditorUserWeb,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    dependent_ids: Annotated[list[str], Form(default_factory=list)],
) -> HTMLResponse:
    added, removed = await ChecksViewService.set_dependents(db, check_id, dependent_ids)
    msg = f"Updated dependents: +{added} −{removed}"
    return hx_empty_with_toast(
        msg, extra_events={"closeSidePanel": {}, "refreshPage": {}}
    )


# ---- Bulk operations ----------------------------------------------------


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.post(
    "/checks/bulk-action",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="bulk_action",
)
async def bulk_action(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: EditorUserWeb,
    action: Annotated[str, Form()],
    select_all: Annotated[str, Form()],
    check_ids: Annotated[list[UUID], Form(default_factory=list)],
    agent: OptionalIdForm = None,
    enabled: Annotated[str, Form()] = "all",
    tag: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Perform bulk action on checks (delete, disable, enable).

    `action=delete` enqueues a maintenance job (cascade through check_results
    runs in the worker, not the web request). Other actions
    stay synchronous — they're in-place UPDATEs, no cascade.
    """
    # Background-friendly cases (cascade or large N) → enqueue maintenance
    # job; UI polls /maintenance/{id}/status via OOB swap into the slot.
    if action == "delete" or (action in ("enable", "disable") and select_all == "true"):
        job, resolved_count, msg = await ChecksViewService.enqueue_bulk_check_action(
            db,
            action,
            select_all,
            agent,
            enabled,
            tag,
            check_ids,
            owner_id=current_user.id,
        )
        return job_status_oob_response(request, current_user, job, msg)

    # Small N enable/disable: stay synchronous.
    (
        _success,
        _failure,
        message,
        toast_kind,
    ) = await ChecksViewService.execute_bulk_action(
        db, action, select_all, agent, enabled, tag, check_ids
    )
    oob_context = await StatusViewService.build_oob_status_context(
        db, request, current_user
    )
    return bulk_oob_response(oob_context, message, toast_kind)


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.post(
    "/checks/bulk-modify",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="bulk_modify",
)
async def bulk_modify(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: EditorUserWeb,
    select_all: Annotated[str, Form()],
    check_ids: Annotated[list[UUID], Form(default_factory=list)],
    agent: OptionalIdForm = None,
    enabled: Annotated[str, Form()] = "all",
    tag: Annotated[str, Form()] = "",
    interval: Annotated[str, Form()] = "",
    timeout: Annotated[str, Form()] = "",
    retry_attempts: Annotated[str, Form()] = "",
    agent_id: OptionalIdForm = None,
    alert_id: OptionalIdForm = None,
) -> HTMLResponse:
    """Enqueue a bulk_check_modify maintenance job."""
    interval_int = int(interval) if interval else None
    timeout_int = int(timeout) if timeout else None
    retry_attempts_int = int(retry_attempts) if retry_attempts else None
    update_data = ChecksViewService.build_bulk_update_data(
        interval_int, timeout_int, retry_attempts_int
    )
    job, count = await ChecksViewService.enqueue_bulk_modify(
        db,
        select_all,
        agent,
        enabled,
        tag,
        check_ids,
        update_data,
        agent_id,
        alert_id,
        owner_id=current_user.id,
    )
    return job_status_oob_response(
        request, current_user, job, f"Modifying {count} check(s) in background…"
    )


# route-smoke: requested by the route-table sweep (tests/route_smoke)
@router.post(
    "/checks/bulk-preview",
    response_class=HTMLResponse,
    include_in_schema=False,
    name="bulk_check_preview",
)
async def bulk_check_preview(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: EditorUserWeb,
    bulk_urls: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Generate preview of bulk check names from URLs and validate them."""
    context = await ChecksViewService.build_bulk_preview_context(
        db, request, current_user, bulk_urls
    )
    if context is None:
        return HTMLResponse(content="", status_code=200)
    return templates.TemplateResponse(request, "partials/bulk_preview.html", context)
