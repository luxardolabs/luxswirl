"""Shared HTMX render helpers for web routers.

Web routers must contain ONLY route handlers (LUXSWIRL-172) — the small,
repeated "render an error/status partial" helpers that used to be duplicated as
module-level functions in each router live here instead. This module is not a
router, so it may import the User model and own these `templates.TemplateResponse`
builders.
"""

from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.user_model import User
from app.services.views.notification_logs_view_service import NotificationLogsViewService
from app.web._hx_responses import hx_toast_trigger, hx_trigger
from app.web.templates_config import templates


def error_partial(
    request: Request,
    current_user: User | None,
    error: str,
    status_code: int = status.HTTP_400_BAD_REQUEST,
    template: str = "partials/error_message.html",
) -> Response:
    """Render an error partial (HTMX swap target). `template` defaults to the
    management error card; public status pages pass the simpler partials/error.html."""
    return templates.TemplateResponse(
        request,
        template,
        {"request": request, "current_user": current_user, "error": error},
        status_code=status_code,
    )


def error_page(
    request: Request,
    current_user: User | None,
    error: str,
    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR,
    page_title: str = "Error",
) -> Response:
    """Render the full error page."""
    return templates.TemplateResponse(
        request,
        "pages/error.html",
        {"current_user": current_user, "error": error, "page_title": page_title},
        status_code=status_code,
    )


def public_error_page(
    request: Request,
    error: str,
    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR,
    *,
    title: str = "Something went wrong",
) -> Response:
    """Render a branded error on the public status layout — no admin sidebar /
    current_user (unlike error_page). For the anonymous public status route."""
    return templates.TemplateResponse(
        request,
        "pages/public_error.html",
        {"error": error, "error_title": title, "page_title": title, "status_page": None},
        status_code=status_code,
    )


def render_error_response(
    request: Request,
    status_code: int,
    message: str,
    *,
    error_code: str = "INTERNAL_ERROR",
    detail: Any = None,
) -> Response:
    """Content-negotiated error response — the single place that turns an
    (status, message) into a response, matching the fleet pattern (luxwx
    `create_error_response`, boutique `render_error_response`):

    - API request (path under /api/ or Accept: application/json) -> JSON
    - HTMX request (HX-Request: true)  -> empty body + showToast toast, no swap
    - Web request                      -> full HTML error page

    main.py's exception handlers call this so routers and services can raise
    and never render errors (or roll back) themselves. The HTMX branch returns
    an empty body with HX-Reswap=none so the failed action's target is left
    intact and the user just sees an error toast.
    """
    wants_json = request.url.path.startswith("/api/") or (
        "application/json" in request.headers.get("accept", "")
    )
    if wants_json:
        content: dict[str, Any] = {"error": error_code, "message": message}
        if detail is not None:
            content["detail"] = detail
        return JSONResponse(status_code=status_code, content=content)

    if request.headers.get("HX-Request") == "true":
        return HTMLResponse(
            content="",
            status_code=status_code,
            headers={
                "HX-Trigger": hx_toast_trigger(message, kind="error"),
                "HX-Reswap": "none",
            },
        )

    return error_page(request, None, message, status_code=status_code)


def status_message(
    request: Request,
    template: str,
    kind: str,
    message: str,
    status_code: int = status.HTTP_200_OK,
    extra: dict[str, Any] | None = None,
) -> Response:
    """Render a `{kind, message}` status-message partial at `template`."""
    context: dict[str, Any] = {"kind": kind, "message": message}
    if extra:
        context.update(extra)
    return templates.TemplateResponse(request, template, context, status_code=status_code)


def toast_partial(
    request: Request,
    template: str,
    context: dict[str, Any],
    message: str,
    status_code: int = status.HTTP_200_OK,
) -> Response:
    """Render a partial with an HX-Trigger toast (used for inline row re-renders)."""
    return templates.TemplateResponse(
        request,
        template,
        context,
        status_code=status_code,
        headers={"HX-Trigger": hx_toast_trigger(message)},
    )


def job_status_oob_response(
    request: Request, current_user: User, job: Any, message: str, toast_kind: str = "success"
) -> HTMLResponse:
    """Render the maintenance job_status partial into the OOB status slot + a toast."""
    partial_html = templates.get_template("partials/maintenance/job_status.html").render(
        job=job, request=request, current_user=current_user
    )
    oob_body = f'<div id="maintenance-status-slot" hx-swap-oob="innerHTML">{partial_html}</div>'
    return HTMLResponse(
        content=oob_body,
        status_code=200,
        headers={
            "HX-Trigger": hx_trigger(
                {"showToast": {"message": message, "type": toast_kind}, "bulkActionComplete": None}
            )
        },
    )


def bulk_oob_response(oob_context: dict, message: str, toast_kind: str) -> HTMLResponse:
    """Render the OOB-swap bulk-response template + HX-Trigger toast headers."""
    body = templates.get_template("partials/checks/bulk_oob_response.html").render(**oob_context)
    return HTMLResponse(
        content=body,
        status_code=200,
        headers={
            "HX-Trigger": hx_trigger(
                {
                    "showToast": {"message": message, "type": toast_kind},
                    "bulkActionComplete": None,
                }
            )
        },
    )


def bulk_check_response(
    request: Request,
    current_user: User,
    result: Any,
    error: str | None,
    success_label: str,
) -> Response:
    """Render the bulk-check success/error partial after a JobBulkCheck call."""
    if error:
        return error_partial(
            request,
            current_user,
            error,
            status_code=404 if "not found" in error.lower() else 400,
        )
    assert result is not None
    message = f"✓ Created {result.created_count} {success_label}"
    if result.skipped_count > 0:
        message += f", skipped {result.skipped_count} existing"
    return templates.TemplateResponse(
        request,
        "partials/success_message.html",
        {"current_user": current_user, "message": message},
    )


def render_setup(
    request: Request,
    *,
    username: str = "",
    error: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> Response:
    """Render the setup page, pre-filling sensible defaults."""
    return cast(
        Response,
        templates.TemplateResponse(
            request,
            "pages/setup.html",
            {
                "current_user": None,
                "username": username or settings.security.initial_admin_username,
                "error": error,
            },
            status_code=status_code,
        ),
    )


async def render_log_row(
    request: Request, db: AsyncSession, log_id: UUID, message: str
) -> HTMLResponse:
    """Re-render a single notification log row partial with an HX-Trigger toast."""
    log = await NotificationLogsViewService.get_log_row_by_id(db, log_id)
    if log is None:
        raise HTTPException(status_code=404, detail="Notification log not found")
    return templates.TemplateResponse(
        request,
        "partials/notification_logs/log_row.html",
        {"request": request, "log": log},
        headers={"HX-Trigger": hx_toast_trigger(message)},
    )
