"""
Settings router — web UI for application settings.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from shared.logger import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_deps import AdminUserWeb
from app.core.query_params import EnabledFilter, GlobalFilter, PerPageFilter
from app.db import get_db
from app.services.views.settings_view_service import SettingsViewService
from app.web.hx_responses import hx_empty_with_toast, hx_toast_trigger
from app.web.render import error_partial
from app.web.templates_config import templates

logger = get_logger("luxswirl.web.settings")

router = APIRouter(tags=["Web UI - Settings"], include_in_schema=False)


# ---- Pages --------------------------------------------------------------


@router.get("/settings", response_class=HTMLResponse)
async def settings_landing(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: AdminUserWeb,
):
    """Settings landing page."""
    context = await SettingsViewService.build_landing_context(db, request, current_user)
    return templates.TemplateResponse(request, "pages/settings/index.html", context)


@router.get("/settings/notifications", response_class=HTMLResponse)
async def settings_notifications(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: AdminUserWeb,
):
    """Notification providers settings page."""
    context = await SettingsViewService.build_notifications_context(
        db, request, current_user
    )
    return templates.TemplateResponse(
        request, "pages/settings/notifications.html", context
    )


@router.get("/settings/alerts", response_class=HTMLResponse)
async def settings_alerts(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: AdminUserWeb,
    is_enabled: EnabledFilter = None,
    is_global: GlobalFilter = None,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: PerPageFilter = None,
):
    """Alert rules settings page."""
    context = await SettingsViewService.build_alerts_context(
        db, request, current_user, is_enabled, is_global, page, per_page
    )
    return templates.TemplateResponse(request, "pages/settings/alerts.html", context)


@router.get("/settings/registration-keys", response_class=HTMLResponse)
async def settings_registration_keys(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: AdminUserWeb,
    is_enabled: EnabledFilter = None,
):
    """Registration keys + Prometheus metrics settings page."""
    context = await SettingsViewService.build_registration_keys_context(
        db, request, current_user, is_enabled
    )
    return templates.TemplateResponse(
        request, "pages/settings/registration_keys.html", context
    )


@router.get("/settings/components", response_class=HTMLResponse)
async def settings_components(
    request: Request,
    current_user: AdminUserWeb,
):
    """Component library / brand showcase page (no DB)."""
    return templates.TemplateResponse(
        request,
        "pages/settings/components.html",
        SettingsViewService.build_components_context(request, current_user),
    )


@router.get("/settings/logo-demo", response_class=HTMLResponse)
async def settings_logo_demo(
    request: Request,
    current_user: AdminUserWeb,
):
    """Logo animation design playground (admin-only, no DB)."""
    return templates.TemplateResponse(
        request,
        "pages/logo-demo.html",
        {"request": request, "current_user": current_user},
    )


@router.get("/settings/defaults", response_class=HTMLResponse)
async def settings_defaults(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: AdminUserWeb,
):
    """System defaults settings page — all configurable defaults."""
    context = await SettingsViewService.build_defaults_context(
        db, request, current_user
    )
    return templates.TemplateResponse(request, "pages/settings/defaults.html", context)


# ---- HTMX setting card mutations ----------------------------------------


@router.post("/settings/{key}/update", response_class=HTMLResponse)
async def update_setting(
    request: Request,
    key: str,
    value: Annotated[str, Form()],
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: AdminUserWeb,
):
    """Update a setting value; returns the refreshed setting card."""
    try:
        updated_setting, error = await SettingsViewService.update_setting_with_parsing(
            db, key, value
        )
        if updated_setting is None:
            # `error` is always set on the failure path; narrowing on the VALUE (not the
            # message) is what makes that contract checkable.
            message = error or "Could not update setting"
            return hx_empty_with_toast(
                message,
                kind="error",
                status_code=404 if "not found" in message.lower() else 400,
            )
        response = templates.TemplateResponse(
            request,
            "partials/settings/setting_card.html",
            SettingsViewService.build_setting_card_context(
                request, current_user, updated_setting
            ),
        )
        response.headers["HX-Trigger"] = hx_toast_trigger(
            f"Setting '{updated_setting.display_name}' saved"
        )
        return response
    except ValueError as e:
        logger.exception(
            "Validation error updating setting",
            extra={"setting_key": key},
        )
        return hx_empty_with_toast(
            f"Validation error: {e}", kind="error", status_code=400
        )


@router.post("/settings/{key}/reset", response_class=HTMLResponse)
async def reset_setting(
    request: Request,
    key: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: AdminUserWeb,
):
    """Reset a setting to its default; returns the refreshed setting card."""
    updated_setting = await SettingsViewService.reset_setting_with_cache_update(db, key)
    response = templates.TemplateResponse(
        request,
        "partials/settings/setting_card.html",
        SettingsViewService.build_setting_card_context(
            request, current_user, updated_setting
        ),
    )
    response.headers["HX-Trigger"] = hx_toast_trigger(
        f"Setting '{updated_setting.display_name}' reset to default"
    )
    return response


# ---- Metrics card mutations ---------------------------------------------


@router.post("/settings/metrics/generate-token", response_class=HTMLResponse)
async def generate_metrics_token(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: AdminUserWeb,
):
    """Generate new Prometheus metrics bearer token (shown once)."""
    return await SettingsViewService.render_new_metrics_token(db, request, current_user)


@router.post("/settings/metrics/toggle-enabled", response_class=HTMLResponse)
async def toggle_metrics_enabled(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: AdminUserWeb,
):
    """Toggle Prometheus metrics endpoint on/off."""
    metrics_settings = await SettingsViewService.toggle_metrics_enabled(db)
    return templates.TemplateResponse(
        request,
        "partials/settings/metrics_card.html",
        SettingsViewService.build_metrics_card_context(
            request, current_user, metrics_settings
        ),
    )


@router.post("/settings/metrics/toggle-auth", response_class=HTMLResponse)
async def toggle_metrics_auth(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: AdminUserWeb,
):
    """Toggle Prometheus metrics authentication on/off."""
    metrics_settings = await SettingsViewService.toggle_metrics_auth(db)
    return templates.TemplateResponse(
        request,
        "partials/settings/metrics_card.html",
        SettingsViewService.build_metrics_card_context(
            request, current_user, metrics_settings
        ),
    )


@router.post("/settings/metrics/update-timeout", response_class=HTMLResponse)
async def update_metrics_timeout(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: AdminUserWeb,
):
    """Update agent timeout threshold."""
    form_data = await request.form()
    timeout_str = str(form_data.get("timeout") or "")
    metrics_settings, error = await SettingsViewService.update_metrics_timeout(
        db, timeout_str
    )
    if error:
        return error_partial(request, current_user, error)
    return templates.TemplateResponse(
        request,
        "partials/settings/metrics_card.html",
        SettingsViewService.build_metrics_card_context(
            request, current_user, metrics_settings
        ),
    )
