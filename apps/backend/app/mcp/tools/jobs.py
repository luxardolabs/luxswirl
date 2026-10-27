"""Job tools: agent jobs (network scan and discover) and background-job status.

Run a scan on an agent, read its result, and turn its hosts into checks: each wraps
the call the matching /api/v1/admin/jobs route makes.
"""

from typing import Any

from mcp.types import TextContent, Tool

from app.mcp.context import get_db
from app.mcp.formatting import json_response
from app.mcp.tools._base import (
    PAGE_PROPERTIES,
    body_schema,
    choice,
    page_args,
    principal,
)
from app.mcp.validators import parse_body, validate_optional_uuid, validate_uuid
from app.models.enum_model import JobStatus, JobType
from app.schemas.admin_job_schema import AdminJobChecks, AdminJobCreate
from app.services.core.admin_job_core_service import AdminJobCoreService
from app.services.core.maintenance_job_core_service import MaintenanceJobCoreService

_JOB_ID = {"job_id": {"type": "string", "description": "Job UUID"}}

TOOLS: list[Tool] = [
    Tool(
        name="list_jobs",
        description=(
            "One page of agent jobs (network scans and discoveries), highest priority "
            "first, then newest."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **PAGE_PROPERTIES,
                "status": {"type": "string", "enum": [s.value for s in JobStatus]},
                "job_type": {"type": "string", "enum": [t.value for t in JobType]},
                "agent_id": {"type": "string", "description": "Only this agent's jobs"},
            },
        },
    ),
    Tool(
        name="get_job",
        description="One agent job, with its result once it has finished.",
        inputSchema={"type": "object", "properties": _JOB_ID, "required": ["job_id"]},
    ),
    Tool(
        name="list_job_types",
        description=(
            "Each job type: what it does, whether it needs an agent, and the JSON "
            "Schema of the params create_job takes for it."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="create_job",
        description=(
            "Queue a job on an agent: the same body as POST /api/v1/admin/jobs. The "
            "agent picks it up on its next heartbeat; poll get_job for the result."
        ),
        inputSchema=body_schema(AdminJobCreate),
    ),
    Tool(
        name="create_checks_from_job",
        description=(
            "Make one kind of check (ping, web, ssh, database) from a finished scan's "
            "hosts, on an agent: `checks` is the same body as POST "
            "/api/v1/admin/jobs/{id}/checks. A host whose check exists is skipped."
        ),
        inputSchema={
            "type": "object",
            "properties": {**_JOB_ID, "checks": body_schema(AdminJobChecks)},
            "required": ["job_id", "checks"],
        },
    ),
    Tool(
        name="get_maintenance_job",
        description=(
            "A background job's status and progress (a bulk change, an import, a "
            "delete), by the id a change returned."
        ),
        inputSchema={"type": "object", "properties": _JOB_ID, "required": ["job_id"]},
    ),
]


async def handle_list_jobs(arguments: dict[str, Any]) -> list[TextContent]:
    page, per_page = page_args(arguments)
    return json_response(
        await AdminJobCoreService.list_jobs(
            get_db(),
            page=page,
            per_page=per_page,
            status=choice(arguments.get("status"), JobStatus, "status"),
            job_type=choice(arguments.get("job_type"), JobType, "job_type"),
            agent_id=validate_optional_uuid(arguments.get("agent_id"), "agent_id"),
        )
    )


async def handle_get_job(arguments: dict[str, Any]) -> list[TextContent]:
    return json_response(
        await AdminJobCoreService.get_job(
            get_db(), validate_uuid(arguments.get("job_id"), "job_id")
        )
    )


async def handle_list_job_types(arguments: dict[str, Any]) -> list[TextContent]:
    return json_response(AdminJobCoreService.job_types())


async def handle_create_job(arguments: dict[str, Any]) -> list[TextContent]:
    data = parse_body(AdminJobCreate, arguments)
    return json_response(
        await AdminJobCoreService.create_job(get_db(), principal().user.username, data)
    )


async def handle_create_checks_from_job(arguments: dict[str, Any]) -> list[TextContent]:
    job_id = validate_uuid(arguments.get("job_id"), "job_id")
    data = parse_body(AdminJobChecks, arguments.get("checks") or {})
    return json_response(
        await AdminJobCoreService.create_checks(get_db(), job_id, data)
    )


async def handle_get_maintenance_job(arguments: dict[str, Any]) -> list[TextContent]:
    return json_response(
        await MaintenanceJobCoreService.get_by_id(
            get_db(), validate_uuid(arguments.get("job_id"), "job_id")
        )
    )


HANDLERS = {
    "list_jobs": handle_list_jobs,
    "get_job": handle_get_job,
    "list_job_types": handle_list_job_types,
    "create_job": handle_create_job,
    "create_checks_from_job": handle_create_checks_from_job,
    "get_maintenance_job": handle_get_maintenance_job,
}
