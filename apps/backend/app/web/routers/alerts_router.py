"""
Alerts router - web UI for managing alert rules.

Provides interface for creating, editing, and managing alert rules
that trigger notifications based on check status changes.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.services.views.alert_form_view_service import AlertFormViewService
from app.services.views.alerts_view_service import AlertsViewService
from app.services.views.checks_view_service import ChecksViewService
from app.web.deps import CurrentUserWeb, EditorUserWeb
from app.web.hx_responses import hx_close_panel_and_refresh, hx_empty_with_toast
from app.web.render import render_log_row
from app.web.templates import templates

logger = get_logger("luxswirl.web.alerts")

router = APIRouter(tags=["Web UI - Alerts"], include_in_schema=False)


@router.get("/alerts/create-form", response_class=HTMLResponse)
async def create_alert_form(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUserWeb,
) -> HTMLResponse:
    """HTMX partial - returns form for creating a new alert."""
    form_data = await AlertsViewService.get_alert_form_data(db, request)

    # Get form defaults

    defaults = await ChecksViewService.get_form_defaults(db)

    return templates.TemplateResponse(
        request,
        "partials/forms/alerts/alert_form.html",
        {
            "current_user": current_user,
            **form_data,
            **defaults,
        },
    )


@router.get("/alerts/{alert_id}/edit-form", response_class=HTMLResponse)
async def edit_alert_form(
    request: Request,
    alert_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUserWeb,
) -> HTMLResponse:
    """HTMX partial - returns form for editing an existing alert."""
    form_data = await AlertsViewService.get_alert_form_data(db, request, alert_id)

    # Get form defaults

    defaults = await ChecksViewService.get_form_defaults(db)

    return templates.TemplateResponse(
        request,
        "partials/forms/alerts/alert_form.html",
        {
            "current_user": current_user,
            **form_data,
            **defaults,
        },
    )


@router.post("/alerts/create", response_class=HTMLResponse, name="web_create_alert")
async def create_alert(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
) -> HTMLResponse:
    """Create a new alert rule."""
    form = await request.form()

    logger.info(
        "CREATE ALERT form submitted",
        extra={
            "name": str(form.get("name", "")),
            "trigger_type": str(form.get("trigger_type", "")),
            "user_id": str(current_user.id),
        },
    )

    # Extract basic fields
    name = str(form.get("name", ""))
    description = str(form.get("description", "")).strip() or None
    trigger_type = str(form.get("trigger_type", ""))
    is_enabled = form.get("is_enabled") == "true"
    is_global = form.get("is_global") == "true"
    notify_on_recovery = form.get("notify_on_recovery") == "true"
    custom_subject = str(form.get("custom_subject", "")).strip() or None
    custom_message = str(form.get("custom_message", "")).strip() or None

    # Handle optional integers
    resend_interval_str = form.get("resend_interval_minutes", "")
    resend_interval_minutes = (
        int(str(resend_interval_str)) if resend_interval_str else None
    )

    max_resends_str = form.get("max_resends", "")
    max_resends = (
        int(str(max_resends_str))
        if max_resends_str and str(max_resends_str) != "0"
        else None
    )

    # Parse form data for providers, checks, and thresholds using service
    form_data = AlertFormViewService.parse_alert_form(dict(form))
    provider_ids = form_data.provider_ids
    check_id_list = form_data.check_ids

    # Build trigger config from form fields
    trigger_config = {}
    if trigger_type == "status_change":
        consecutive_failures = form.get("trigger_consecutive_failures", "3")
        trigger_config = {
            "on_status": ["error"],
            "consecutive_failures": int(str(consecutive_failures)),
        }
    elif trigger_type == "threshold":
        threshold_metric = form.get("trigger_threshold_metric", "latency_ms")
        threshold_operator = form.get("trigger_threshold_operator", ">")
        threshold_value = form.get("trigger_threshold_value", "1000")
        trigger_config = {
            "metric": threshold_metric,
            "operator": threshold_operator,
            "value": int(str(threshold_value)),
        }
    elif trigger_type == "ssl_cert_expiry":
        # Use parsed thresholds or default to 30 days
        days_thresholds = form_data.days_thresholds or [30]
        trigger_config = {"days_thresholds": days_thresholds}

    # Debug logging
    logger.info(
        "Alert creation form",
        extra={
            "is_global": is_global,
            "is_global_raw": form.get("is_global"),
            "check_ids": [str(c) for c in check_id_list],
            "provider_ids": [str(p) for p in provider_ids],
        },
    )

    # Note: It's OK to create a specific alert with no checks assigned initially.
    # The user can assign checks later via the checks UI or API.
    # An alert with is_global=false and no check mappings simply won't trigger
    # until checks are assigned to it.

    # Create alert (DTO built in the view seam — LUXSWIRL-168)
    alert = await AlertsViewService.create_alert(
        db,
        name=name,
        description=description or None,
        trigger_type=trigger_type,
        trigger_config=trigger_config,
        is_enabled=is_enabled,
        is_global=is_global,
        notify_on_recovery=notify_on_recovery,
        resend_interval_minutes=resend_interval_minutes,
        max_resends=max_resends,
        custom_subject=custom_subject or None,
        custom_message=custom_message or None,
        notification_provider_ids=provider_ids,
        check_ids=check_id_list,
    )

    logger.info(
        "Created alert",
        extra={"alert_name": alert.name, "alert_id": str(alert.id)},
    )

    # Return success
    return hx_close_panel_and_refresh()


# The literal /alerts/snooze routes are declared BEFORE the /alerts/{alert_id}
# ones: Starlette matches in registration order, so a parameterised path
# registered first swallows the literal one — DELETE /alerts/snooze was reaching
# delete_alert with alert_id="snooze" and 422ing, which made unsnooze
# unreachable.
@router.post("/alerts/snooze", response_class=HTMLResponse)
async def snooze_alert_check(
    request: Request,
    alert_id: Annotated[UUID, Query(description="Alert UUID")],
    check_id: Annotated[UUID, Query(description="Check UUID")],
    log_id: Annotated[
        UUID, Query(description="Notification log UUID for the row to refresh")
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUserWeb,
) -> HTMLResponse:
    """
    Snooze an alert-check relationship for 15 more minutes.

    Returns the re-rendered notification log row partial (HTMX swap target) and
    an HX-Trigger toast event.
    """
    message = await AlertsViewService.snooze_alert_check(
        db, alert_id, check_id, current_user
    )
    return await render_log_row(request, db, log_id, message)


@router.delete("/alerts/snooze", response_class=HTMLResponse)
async def unsnooze_alert_check(
    request: Request,
    alert_id: Annotated[UUID, Query(description="Alert UUID")],
    check_id: Annotated[UUID, Query(description="Check UUID")],
    log_id: Annotated[
        UUID, Query(description="Notification log UUID for the row to refresh")
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUserWeb,
) -> HTMLResponse:
    """Un-snooze an alert-check relationship — resume notifications immediately."""
    message = await AlertsViewService.unsnooze_alert_check(
        db, alert_id, check_id, current_user
    )
    return await render_log_row(request, db, log_id, message)


@router.post(
    "/alerts/{alert_id}/update", response_class=HTMLResponse, name="web_update_alert"
)
async def update_alert(
    request: Request,
    alert_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
) -> HTMLResponse:
    """Update an existing alert rule."""
    form = await request.form()

    # Extract basic fields
    name = str(form.get("name", ""))
    description = str(form.get("description", "")).strip() or None
    is_enabled = form.get("is_enabled") == "true"
    notify_on_recovery = form.get("notify_on_recovery") == "true"
    custom_subject = str(form.get("custom_subject", "")).strip() or None
    custom_message = str(form.get("custom_message", "")).strip() or None

    # Handle optional integers
    resend_interval_str = form.get("resend_interval_minutes", "")
    resend_interval_minutes = (
        int(str(resend_interval_str)) if resend_interval_str else None
    )

    max_resends_str = form.get("max_resends", "")
    max_resends = (
        int(str(max_resends_str))
        if max_resends_str and str(max_resends_str) != "0"
        else None
    )

    # Build trigger config from form fields (if present)
    # Note: trigger_type itself cannot be changed, only the config values
    trigger_config = None
    if "trigger_consecutive_failures" in form:
        consecutive_failures = form.get("trigger_consecutive_failures", "3")
        trigger_config = {
            "on_status": ["error"],
            "consecutive_failures": int(str(consecutive_failures)),
        }
    elif "trigger_threshold_value" in form:
        threshold_metric = form.get("trigger_threshold_metric", "latency_ms")
        threshold_operator = form.get("trigger_threshold_operator", ">")
        threshold_value = form.get("trigger_threshold_value", "1000")
        trigger_config = {
            "metric": threshold_metric,
            "operator": threshold_operator,
            "value": int(str(threshold_value)),
        }
    elif any(key.startswith("trigger_days_threshold_") for key in form):
        # Collect all selected threshold checkboxes
        days_thresholds = []
        for key in form:
            if key.startswith("trigger_days_threshold_"):
                days_thresholds.append(int(str(form[key])))

        # Sort thresholds for cleaner storage
        days_thresholds.sort()

        # Require at least one threshold
        if not days_thresholds:
            days_thresholds = [30]  # Default fallback

        trigger_config = {"days_thresholds": days_thresholds}

    # Update alert (DTO built in the view seam — LUXSWIRL-168)
    alert = await AlertsViewService.update_alert(
        db,
        alert_id,
        name=name,
        description=description or None,
        is_enabled=is_enabled,
        notify_on_recovery=notify_on_recovery,
        resend_interval_minutes=resend_interval_minutes,
        max_resends=max_resends,
        custom_subject=custom_subject or None,
        custom_message=custom_message or None,
        trigger_config=trigger_config,
    )

    logger.info(
        "Updated alert",
        extra={"alert_name": alert.name, "alert_id": str(alert.id)},
    )

    # Return success
    return hx_close_panel_and_refresh()


@router.delete(
    "/alerts/{alert_id}", response_class=HTMLResponse, name="web_delete_alert"
)
async def delete_alert(
    request: Request,
    alert_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
) -> HTMLResponse:
    """Delete an alert rule (soft delete)."""
    await AlertsViewService.delete_alert(db, alert_id)

    logger.info("Deleted alert", extra={"alert_id": str(alert_id)})

    return hx_empty_with_toast("Alert deleted")


@router.post("/alerts/{alert_id}/toggle", response_class=HTMLResponse)
async def toggle_alert(
    request: Request,
    alert_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
) -> HTMLResponse:
    """Toggle alert enabled/disabled status."""
    return await AlertsViewService.toggle_alert_enabled(db, alert_id)


# Snooze endpoints - manage alert-check relationship notification pausing
