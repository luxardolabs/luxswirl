"""
Checks router — web UI for managing health checks.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Path, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BeforeValidator
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_deps import (
    CurrentUserWeb,
    EditorUserWeb,
)
from app.core.exceptions import CheckNotFoundException
from app.core.query_params import AgentIdFilter, CheckTypeFilter, empty_to_none
from app.db import get_db
from app.services.views.checks_view_service import ChecksViewService
from app.services.views.status_view_service import StatusViewService
from app.web._hx_responses import hx_empty_with_toast
from app.web.routers._render import bulk_oob_response, error_partial, job_status_oob_response
from app.web.templates_config import templates

logger = get_logger("luxswirl.web.routers.checks")

router = APIRouter(tags=["Web UI - Checks"])

# Optional UUID form field: an unselected dropdown posts "" — normalize to None
# (empty_to_none) before UUID validation, mirroring the query-param filters.
OptionalIdForm = Annotated[UUID | None, BeforeValidator(empty_to_none), Form()]


# ---- HTMX selectors / forms ---------------------------------------------


@router.get(
    "/checks/assignment-mode-selector",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def assignment_mode_selector(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUserWeb,
    assignment_mode: Annotated[str, Query()] = "manual",
):
    """Return assignment selector UI based on mode."""
    context = await ChecksViewService.build_assignment_mode_selector_context(
        db, request, current_user, assignment_mode
    )
    if context is None:
        return HTMLResponse(content="")
    return templates.TemplateResponse(request, "partials/check_assignment_selector.html", context)


@router.get("/checks/create-form", response_class=HTMLResponse, include_in_schema=False)
async def create_form(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUserWeb,
):
    """Get the create form for a new check."""
    context = await ChecksViewService.build_create_form_context(db, request, current_user)
    return templates.TemplateResponse(request, "panels/checks/check_form_panel.html", context)


@router.get("/checks/{check_id}/edit-form", response_class=HTMLResponse, include_in_schema=False)
async def edit_form(
    request: Request,
    check_id: Annotated[UUID, Path(description="Check UUID")],
    current_user: CurrentUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get the edit form for an existing check."""
    try:
        context = await ChecksViewService.build_edit_form_context(
            db, request, current_user, check_id
        )
        return templates.TemplateResponse(request, "panels/checks/check_form_panel.html", context)
    except CheckNotFoundException as e:
        logger.error("Check not found", exc_info=True)
        return error_partial(request, current_user, str(e), 404)


@router.get("/checks/{check_id}/clone-form", response_class=HTMLResponse, include_in_schema=False)
async def clone_form(
    request: Request,
    check_id: Annotated[UUID, Path(description="Check UUID to clone")],
    current_user: CurrentUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get the clone form for an existing check."""
    try:
        context = await ChecksViewService.build_clone_form_context(
            db, request, current_user, check_id
        )
        return templates.TemplateResponse(request, "panels/checks/check_form_panel.html", context)
    except CheckNotFoundException as e:
        logger.error("Check not found", exc_info=True)
        return error_partial(request, current_user, str(e), 404)


# ---- Mutations ----------------------------------------------------------


@router.post("/checks/{check_id}/clone", response_class=HTMLResponse, include_in_schema=False)
async def clone_check_handler(
    request: Request,
    check_id: Annotated[UUID, Path(description="Check UUID to clone")],
    current_user: EditorUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
    agent_id: Annotated[UUID, Form()],
    display_name: Annotated[str, Form()],
    check_type: Annotated[str, Form()],
    target: Annotated[str, Form()],
    enabled: Annotated[bool, Form()] = False,
    interval: Annotated[int, Form()] = 60,
    tags: Annotated[str, Form()] = "",
    http_method: Annotated[str | None, Form()] = None,
    expected_status: Annotated[int | None, Form()] = None,
    json_path: Annotated[str | None, Form()] = None,
    expected_value: Annotated[str | None, Form()] = None,
    script_code: Annotated[str | None, Form()] = None,
    alert_ids: Annotated[list[UUID], Form()] = [],  # noqa: B006
    assignment_mode: Annotated[str, Form()] = "manual",
    agent_selector: Annotated[str | None, Form()] = None,
    description: Annotated[str, Form()] = "",
    timeout_seconds: Annotated[str, Form()] = "",
    verify_ssl: Annotated[bool, Form()] = True,
    retry_attempts: Annotated[str, Form()] = "",
    retry_interval_seconds: Annotated[str, Form()] = "",
    resend_notification_after: Annotated[str, Form()] = "",
    depends_on_check_id: OptionalIdForm = None,
):
    """Create a clone of an existing check."""
    return await ChecksViewService.handle_clone_check_form(
        db,
        request,
        check_id,
        current_user,
        {
            "agent_id": agent_id,
            "display_name": display_name,
            "check_type": check_type,
            "target": target,
            "enabled": enabled,
            "interval": interval,
            "tags": tags,
            "http_method": http_method,
            "expected_status": expected_status,
            "json_path": json_path,
            "expected_value": expected_value,
            "script_code": script_code,
            "alert_ids": alert_ids,
            "assignment_mode": assignment_mode,
            "agent_selector": agent_selector,
            "description": description,
            "timeout_seconds": timeout_seconds,
            "verify_ssl": verify_ssl,
            "retry_attempts": retry_attempts,
            "retry_interval_seconds": retry_interval_seconds,
            "resend_notification_after": resend_notification_after,
            "depends_on_check_id": depends_on_check_id,
        },
    )


@router.post("/checks/create", response_class=HTMLResponse, include_in_schema=False)
async def create_check_assignment(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
    agent_id: Annotated[UUID, Form()],
    check_type: Annotated[str, Form()],
    display_name: Annotated[str, Form()] = "",
    target: Annotated[str, Form()] = "",
    enabled: Annotated[bool, Form()] = False,
    interval: Annotated[int, Form()] = 60,
    tags: Annotated[str, Form()] = "",
    http_method: Annotated[str | None, Form()] = None,
    expected_status: Annotated[int | None, Form()] = None,
    json_path: Annotated[str | None, Form()] = None,
    expected_value: Annotated[str | None, Form()] = None,
    script_code: Annotated[str | None, Form()] = None,
    created_by: Annotated[str | None, Form()] = None,
    alert_ids: Annotated[list[UUID], Form()] = [],  # noqa: B006
    assignment_mode: Annotated[str, Form()] = "manual",
    agent_selector: Annotated[str | None, Form()] = None,
    bulk_urls: Annotated[str, Form()] = "",
    record_type: Annotated[str | None, Form()] = None,
    nameserver: Annotated[str | None, Form()] = None,
    port: Annotated[str | None, Form()] = None,
    expect_value: Annotated[str | None, Form()] = None,
    connection_string: Annotated[str | None, Form()] = None,
    query: Annotated[str | None, Form()] = None,
    description: Annotated[str, Form()] = "",
    timeout_seconds: Annotated[str, Form()] = "",
    verify_ssl: Annotated[bool, Form()] = True,
    retry_attempts: Annotated[str, Form()] = "",
    retry_interval_seconds: Annotated[str, Form()] = "",
    resend_notification_after: Annotated[str, Form()] = "",
    depends_on_check_id: OptionalIdForm = None,
):
    """Create a new check (or bulk import if check_type is http-bulk)."""
    return await ChecksViewService.handle_create_check_form(
        db,
        request,
        current_user,
        {
            "agent_id": agent_id,
            "display_name": display_name,
            "check_type": check_type,
            "target": target,
            "enabled": enabled,
            "interval": interval,
            "tags": tags,
            "http_method": http_method,
            "expected_status": expected_status,
            "json_path": json_path,
            "expected_value": expected_value,
            "script_code": script_code,
            "alert_ids": alert_ids,
            "assignment_mode": assignment_mode,
            "agent_selector": agent_selector,
            "record_type": record_type,
            "nameserver": nameserver,
            "port": port,
            "expect_value": expect_value,
            "connection_string": connection_string,
            "query": query,
            "description": description,
            "timeout_seconds": timeout_seconds,
            "verify_ssl": verify_ssl,
            "retry_attempts": retry_attempts,
            "retry_interval_seconds": retry_interval_seconds,
            "resend_notification_after": resend_notification_after,
            "depends_on_check_id": depends_on_check_id,
        },
    )


@router.post("/checks/{check_id}/update", response_class=HTMLResponse, include_in_schema=False)
async def update_check(
    request: Request,
    check_id: Annotated[UUID, Path(description="Check UUID")],
    current_user: EditorUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
    display_name: Annotated[str, Form()],
    check_type: Annotated[str, Form()],
    target: Annotated[str, Form()],
    interval: Annotated[int, Form()],
    enabled: Annotated[bool, Form()] = False,
    tags: Annotated[str, Form()] = "",
    http_method: Annotated[str | None, Form()] = None,
    expected_status: Annotated[int | None, Form()] = None,
    json_path: Annotated[str | None, Form()] = None,
    expected_value: Annotated[str | None, Form()] = None,
    script_code: Annotated[str | None, Form()] = None,
    alert_ids: Annotated[list[UUID], Form()] = [],  # noqa: B006
    assignment_mode: Annotated[str, Form()] = "manual",
    agent_selector: Annotated[str | None, Form()] = None,
    record_type: Annotated[str | None, Form()] = None,
    nameserver: Annotated[str | None, Form()] = None,
    port: Annotated[str | None, Form()] = None,
    expect_value: Annotated[str | None, Form()] = None,
    connection_string: Annotated[str | None, Form()] = None,
    query: Annotated[str | None, Form()] = None,
    description: Annotated[str, Form()] = "",
    timeout_seconds: Annotated[str, Form()] = "",
    verify_ssl: Annotated[bool, Form()] = True,
    retry_attempts: Annotated[str, Form()] = "",
    retry_interval_seconds: Annotated[str, Form()] = "",
    resend_notification_after: Annotated[str, Form()] = "",
    depends_on_check_id: OptionalIdForm = None,
):
    """Update an existing check."""
    return await ChecksViewService.handle_update_check_form(
        db,
        request,
        check_id,
        current_user,
        {
            "display_name": display_name,
            "check_type": check_type,
            "target": target,
            "enabled": enabled,
            "interval": interval,
            "tags": tags,
            "http_method": http_method,
            "expected_status": expected_status,
            "json_path": json_path,
            "expected_value": expected_value,
            "script_code": script_code,
            "alert_ids": alert_ids,
            "assignment_mode": assignment_mode,
            "agent_selector": agent_selector,
            "record_type": record_type,
            "nameserver": nameserver,
            "port": port,
            "expect_value": expect_value,
            "connection_string": connection_string,
            "query": query,
            "description": description,
            "timeout_seconds": timeout_seconds,
            "verify_ssl": verify_ssl,
            "retry_attempts": retry_attempts,
            "retry_interval_seconds": retry_interval_seconds,
            "resend_notification_after": resend_notification_after,
            "depends_on_check_id": depends_on_check_id,
        },
    )


@router.post("/checks/{check_id}/toggle", response_class=HTMLResponse, include_in_schema=False)
async def toggle_check(
    request: Request,
    check_id: Annotated[UUID, Path(description="Check UUID")],
    current_user: EditorUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Toggle check enabled/disabled status."""
    context = await ChecksViewService.toggle_check_handler(db, check_id)
    return templates.TemplateResponse(request, "partials/checks/toggle_button.html", context)


@router.delete("/checks/{check_id}", response_class=HTMLResponse, include_in_schema=False)
async def delete_check(
    request: Request,
    check_id: Annotated[UUID, Path(description="Check UUID")],
    current_user: EditorUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Delete a check (enqueues a background cascade — see LUXSWIRL-105)."""
    return await ChecksViewService.enqueue_check_delete(db, request, current_user, check_id)


# ---- Dependents management ----------------------------------------------


@router.get(
    "/checks/{check_id}/dependents",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def dependents_panel(
    request: Request,
    check_id: Annotated[UUID, Path(description="Parent check UUID")],
    current_user: EditorUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
    check_type: CheckTypeFilter = None,
    agent_name: Annotated[str, Query()] = "",
    tags: Annotated[str, Query()] = "",
    search: Annotated[str, Query()] = "",
):
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
        return templates.TemplateResponse(request, "panels/checks/dependents_panel.html", context)
    except CheckNotFoundException as e:
        return error_partial(request, current_user, str(e), 404)


@router.post(
    "/checks/{check_id}/dependents",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def update_dependents(
    request: Request,
    check_id: Annotated[UUID, Path(description="Parent check UUID")],
    current_user: EditorUserWeb,
    db: Annotated[AsyncSession, Depends(get_db)],
    dependent_ids: Annotated[list[str], Form()] = [],  # noqa: B006
):
    added, removed = await ChecksViewService.set_dependents(db, check_id, dependent_ids)
    msg = f"Updated dependents: +{added} −{removed}"
    return hx_empty_with_toast(msg, extra_events={"closeSidePanel": {}, "refreshPage": {}})


# ---- Bulk operations ----------------------------------------------------


@router.post("/checks/bulk-action", response_class=HTMLResponse, include_in_schema=False)
async def bulk_action(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
    action: Annotated[str, Form()],
    select_all: Annotated[str, Form()],
    agent: OptionalIdForm = None,
    enabled: Annotated[str, Form()] = "all",
    tag: Annotated[str, Form()] = "",
    check_ids: Annotated[list[UUID], Form()] = [],  # noqa: B006
):
    """Perform bulk action on checks (delete, disable, enable).

    `action=delete` enqueues a maintenance job (cascade through check_results
    runs in the worker, not the web request). See LUXSWIRL-105. Other actions
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
    _success, _failure, message, toast_kind = await ChecksViewService.execute_bulk_action(
        db, action, select_all, agent, enabled, tag, check_ids
    )
    oob_context = await StatusViewService.build_oob_status_context(db, request, current_user)
    return bulk_oob_response(oob_context, message, toast_kind)


@router.post("/checks/bulk-modify", response_class=HTMLResponse, include_in_schema=False)
async def bulk_modify(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
    select_all: Annotated[str, Form()],
    agent: OptionalIdForm = None,
    enabled: Annotated[str, Form()] = "all",
    tag: Annotated[str, Form()] = "",
    check_ids: Annotated[list[UUID], Form()] = [],  # noqa: B006
    interval: Annotated[str, Form()] = "",
    timeout: Annotated[str, Form()] = "",
    retry_attempts: Annotated[str, Form()] = "",
    agent_id: OptionalIdForm = None,
    alert_id: OptionalIdForm = None,
):
    """Enqueue a bulk_check_modify maintenance job. See LUXSWIRL-105."""
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


@router.post("/checks/bulk-preview", response_class=HTMLResponse, include_in_schema=False)
async def bulk_check_preview(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
    bulk_urls: Annotated[str, Form()] = "",
):
    """Generate preview of bulk check names from URLs and validate them."""
    context = await ChecksViewService.build_bulk_preview_context(
        db, request, current_user, bulk_urls
    )
    if context is None:
        return HTMLResponse(content="", status_code=200)
    return templates.TemplateResponse(request, "partials/bulk_preview.html", context)


@router.get("/checks/table", response_class=HTMLResponse, include_in_schema=False)
async def checks_table_partial(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUserWeb,
    agent: AgentIdFilter = None,
    enabled: Annotated[str, Query(description="Filter by enabled status")] = "all",
    page: Annotated[int, Query(ge=1, description="Page number")] = 1,
    per_page: Annotated[
        int | None, Query(ge=10, le=200, description="Items per page (defaults to setting)")
    ] = None,
):
    """Check assignments table partial for HTMX updates."""
    try:
        context = await ChecksViewService.build_table_partial_context(
            db, request, current_user, agent, enabled, page, per_page
        )
        return templates.TemplateResponse(request, "partials/checks_table.html", context)
    except Exception as e:
        logger.error("Error rendering check assignments table", exc_info=True)
        return error_partial(request, current_user, str(e), 500)
