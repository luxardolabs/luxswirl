"""The admin API's OpenAPI document: `/api/v1/admin` alone, with bearer auth.

Served to any API-token holder in every environment, because it is the
document an automation client reads. The app-wide `/openapi.json` (agent
protocol included) stays off in production.
"""

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from app.core.config import settings

ADMIN_API_PREFIX = "/api/v1/admin/"


def admin_openapi_schema(app: FastAPI) -> dict[str, Any]:
    """The OpenAPI document for the admin API's routes in `app`."""
    schema = get_openapi(
        title="LuxSwirl Admin API",
        version=settings.app_version,
        description=(
            "Manage LuxSwirl with a personal API token "
            "(Authorization: Bearer lsw_...). A token acts as its user, "
            "with that user's role."
        ),
        routes=app.routes,
    )
    schema["paths"] = {
        path: item
        for path, item in schema.get("paths", {}).items()
        if path.startswith(ADMIN_API_PREFIX)
    }
    schema.setdefault("components", {})["securitySchemes"] = {
        "apiToken": {"type": "http", "scheme": "bearer"}
    }
    schema["security"] = [{"apiToken": []}]
    return schema
