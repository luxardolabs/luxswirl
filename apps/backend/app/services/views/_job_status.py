"""Shared context builder for the maintenance job_status partial.

Six call sites across three routers and three view services render the same
partial after enqueueing a maintenance job. They share this builder so the
polling URL is resolved from the route name in exactly one place.
"""

from typing import Any

from fastapi import Request

from app.core.auth_deps import AuthedUser
from app.schemas.maintenance_job_schema import MaintenanceJobResponse


def build_job_status_context(
    request: Request, current_user: AuthedUser | None, job: MaintenanceJobResponse
) -> dict[str, Any]:
    """Context for partials/maintenance/job_status.html.

    Args:
        request: Request, for reversing the polling route name into a URL.
        current_user: Authenticated user, or None for unauthenticated renders.
        job: The maintenance job being polled.

    Returns:
        Template context including the finished polling href.
    """
    return {
        "request": request,
        "current_user": current_user,
        "job": job,
        # .path, not str(): url_for returns an absolute url from the request's
        # scheme/host, which behind TLS-terminating nginx can emit http:// into
        # an https:// page.
        "job_status_url": request.url_for("maintenance_job_status", job_id=job.id).path,
    }
