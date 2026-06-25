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

from app.core.auth_deps import (
    CurrentUserWeb,
    EditorUserWeb,
)
from app.db import get_db
from app.services.views.alert_form_view_service import AlertFormViewService
from app.services.views.alerts_view_service import AlertsViewService
from app.services.views.checks_view_service import ChecksViewService
from app.web._hx_responses import hx_empty_with_toast
from app.web.routers._render import render_log_row
from app.web.templates_config import templates

logger = get_logger("luxswirl.web.alerts")

router = APIRouter(tags=["Web UI - Alerts"], include_in_schema=False)


@router.get("/alerts/create-form", response_class=HTMLResponse)
async def create_alert_form(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUserWeb,
):
    """HTMX partial - returns form for creating a new alert."""
    try:
        # Get form data from web service
        form_data = await AlertsViewService.get_alert_form_data(db)

        # Get form defaults

        defaults = await ChecksViewService.get_form_defaults(db)

        return templates.TemplateResponse(
            request,
            "forms/alerts/alert_form.html",
            {
                "current_user": current_user,
                **form_data,
                **defaults,
            },
        )

    except Exception as e:
        logger.error("Error rendering alert create form", exc_info=True)
        return templates.TemplateResponse(
            request,
            "partials/error_message.html",
            {
                "current_user": current_user,
                "error": str(e),
            },
            status_code=500,
        )


@router.get("/alerts/{alert_id}/edit-form", response_class=HTMLResponse)
async def edit_alert_form(
    request: Request,
    alert_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUserWeb,
):
    """HTMX partial - returns form for editing an existing alert."""
    try:
        # Get form data from web service
        form_data = await AlertsViewService.get_alert_form_data(db, alert_id)

        # Get form defaults

        defaults = await ChecksViewService.get_form_defaults(db)

        return templates.TemplateResponse(
            request,
            "forms/alerts/alert_form.html",
            {
                "current_user": current_user,
                **form_data,
                **defaults,
            },
        )

    except Exception as e:
        logger.error("Error rendering alert edit form", exc_info=True)
        return templates.TemplateResponse(
            request,
            "partials/error_message.html",
            {
                "current_user": current_user,
                "error": str(e),
            },
            status_code=500,
        )


@router.post("/alerts/create", response_class=HTMLResponse)
async def create_alert(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
):
    """Create a new alert rule."""
    try:
        # Get form data
        form = await request.form()

        # Debug: log ALL form data
        logger.info(
            "CREATE ALERT form submitted",
            extra={"form_data": dict(form)},
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
        resend_interval_minutes = int(str(resend_interval_str)) if resend_interval_str else None

        max_resends_str = form.get("max_resends", "")
        max_resends = (
            int(str(max_resends_str)) if max_resends_str and str(max_resends_str) != "0" else None
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
        return HTMLResponse(
            content="",
            status_code=200,
            headers={"HX-Trigger": "closeSidePanel,refreshPage"},
        )

    except Exception as e:
        logger.error("Error creating alert", exc_info=True)
        return templates.TemplateResponse(
            request,
            "partials/error_message.html",
            {
                "current_user": current_user,
                "error": str(e),
            },
            status_code=400,
        )


@router.post("/alerts/{alert_id}/update", response_class=HTMLResponse)
async def update_alert(
    request: Request,
    alert_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
):
    """Update an existing alert rule."""
    try:
        # Get form data
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
        resend_interval_minutes = int(str(resend_interval_str)) if resend_interval_str else None

        max_resends_str = form.get("max_resends", "")
        max_resends = (
            int(str(max_resends_str)) if max_resends_str and str(max_resends_str) != "0" else None
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
        elif any(key.startswith("trigger_days_threshold_") for key in form.keys()):
            # Collect all selected threshold checkboxes
            days_thresholds = []
            for key in form.keys():
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
        return HTMLResponse(
            content="",
            status_code=200,
            headers={"HX-Trigger": "closeSidePanel,refreshPage"},
        )

    except Exception as e:
        logger.error("Error updating alert", exc_info=True)
        return templates.TemplateResponse(
            request,
            "partials/error_message.html",
            {
                "current_user": current_user,
                "error": str(e),
            },
            status_code=400,
        )


@router.delete("/alerts/{alert_id}", response_class=HTMLResponse)
async def delete_alert(
    request: Request,
    alert_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
):
    """Delete an alert rule (soft delete)."""
    try:
        await AlertsViewService.delete_alert(db, alert_id)

        logger.info("Deleted alert", extra={"alert_id": str(alert_id)})

        return hx_empty_with_toast("Alert deleted")

    except Exception as e:
        logger.error("Error deleting alert", exc_info=True)
        return templates.TemplateResponse(
            request,
            "partials/error_message.html",
            {
                "current_user": current_user,
                "error": str(e),
            },
            status_code=400,
        )


@router.post("/alerts/{alert_id}/toggle", response_class=HTMLResponse)
async def toggle_alert(
    request: Request,
    alert_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: EditorUserWeb,
):
    """Toggle alert enabled/disabled status."""
    try:
        alert = await AlertsViewService.get_alert_by_id(db, alert_id)

        # Toggle enabled status (partial AlertUpdate built in the view seam)
        alert = await AlertsViewService.set_alert_enabled(db, alert_id, not alert.is_enabled)

        logger.info(
            "Toggled alert",
            extra={
                "alert_name": alert.name,
                "alert_id": str(alert.id),
                "is_enabled": alert.is_enabled,
            },
        )

        # Return empty to trigger refresh
        return HTMLResponse(
            content="",
            status_code=200,
            headers={"HX-Trigger": "refreshPage"},
        )

    except Exception as e:
        logger.error("Error toggling alert", exc_info=True)
        return templates.TemplateResponse(
            request,
            "partials/error_message.html",
            {
                "current_user": current_user,
                "error": str(e),
            },
            status_code=400,
        )


# Snooze endpoints - manage alert-check relationship notification pausing


@router.post("/alerts/snooze", response_class=HTMLResponse)
async def snooze_alert_check(
    request: Request,
    alert_id: Annotated[UUID, Query(description="Alert UUID")],
    check_id: Annotated[UUID, Query(description="Check UUID")],
    log_id: Annotated[UUID, Query(description="Notification log UUID for the row to refresh")],
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUserWeb,
):
    """
    Snooze an alert-check relationship for 15 more minutes.

    Returns the re-rendered notification log row partial (HTMX swap target) and
    an HX-Trigger toast event.
    """
    mapping = await AlertsViewService.snooze_alert_check(db, alert_id, check_id, minutes=15)
    logger.info(
        "User snoozed alert-check relationship",
        extra={
            "username": current_user.username,
            "user_id": str(current_user.id),
            "snoozed_until": str(mapping.snoozed_until),
            "alert_name": mapping.alert.name,
            "alert_id": str(mapping.alert.id),
            "check_name": mapping.check.display_name,
            "check_id": str(mapping.check.id),
        },
    )
    return await render_log_row(request, db, log_id, "Snoozed for 15 minutes")


@router.delete("/alerts/snooze", response_class=HTMLResponse)
async def unsnooze_alert_check(
    request: Request,
    alert_id: Annotated[UUID, Query(description="Alert UUID")],
    check_id: Annotated[UUID, Query(description="Check UUID")],
    log_id: Annotated[UUID, Query(description="Notification log UUID for the row to refresh")],
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUserWeb,
):
    """Un-snooze an alert-check relationship — resume notifications immediately."""
    await AlertsViewService.unsnooze_alert_check(db, alert_id, check_id)
    logger.info(
        "User un-snoozed alert-check relationship",
        extra={"username": current_user.username, "user_id": str(current_user.id)},
    )
    return await render_log_row(request, db, log_id, "Snooze cleared — notifications resumed")
