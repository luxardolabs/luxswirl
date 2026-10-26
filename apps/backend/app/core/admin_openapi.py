"""The admin API's OpenAPI document: `/api/v1/admin` alone, with bearer auth.

Served to any API-token holder in every environment, because it is the
document an automation client reads. It holds the admin API's paths and only
the schemas they reach, so it describes nothing else the app serves. The
app-wide `/openapi.json` (agent protocol included) stays off in production.
"""

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from app.core.config import settings

ADMIN_API_PREFIX = "/api/v1/admin/"


_REF = "#/components/schemas/"


def _referenced(node: Any, schemas: dict[str, Any]) -> set[str]:
    """Every component schema `node` refers to, directly or through another."""
    found: set[str] = set()
    pending = [node]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            ref = item.get("$ref")
            if isinstance(ref, str) and ref.startswith(_REF):
                name = ref.removeprefix(_REF)
                if name not in found and name in schemas:
                    found.add(name)
                    pending.append(schemas[name])
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return found


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
    components = schema.setdefault("components", {})
    all_schemas = components.get("schemas", {})
    components["schemas"] = {
        name: all_schemas[name]
        for name in sorted(_referenced(schema["paths"], all_schemas))
    }
    components["securitySchemes"] = {"apiToken": {"type": "http", "scheme": "bearer"}}
    schema["security"] = [{"apiToken": []}]
    return schema
