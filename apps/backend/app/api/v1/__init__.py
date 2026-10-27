"""
API v1 router aggregation.

Each surface of the JSON API has its own prefix and its own credential:
`/api/v1/agent` is the agent protocol (registration, key recovery, heartbeats,
the agent's own check list, result reports, job results and artifact uploads),
authenticated by the registration key or the agent's own key. `/api/v1/admin`
is the admin API, authenticated by a user's API token with that user's role.
Nothing is mounted on `/api/v1` itself.
"""

from fastapi import APIRouter

from app.api.v1.routers import (
    admin_agent_router,
    admin_alert_router,
    admin_check_result_router,
    admin_check_router,
    admin_identity_router,
    admin_maintenance_job_router,
    admin_notification_log_router,
    admin_notification_provider_router,
    admin_registration_key_router,
    admin_setting_router,
    admin_status_page_router,
    agent_router,
    artifact_router,
    check_router,
    job_router,
    result_router,
)

# The agent protocol: everything an agent calls.
agent_api_router = APIRouter(prefix="/agent")

agent_api_router.include_router(
    agent_router.agent_ops_router
)  # Register, heartbeat, key recovery
agent_api_router.include_router(check_router.router)  # The agent's own check list
agent_api_router.include_router(result_router.router)  # Result reports
agent_api_router.include_router(job_router.router)  # Job start and results
agent_api_router.include_router(artifact_router.router)  # Artifact uploads

# The admin API: everything an operator's token calls.
admin_api_router = APIRouter(prefix="/admin")

admin_api_router.include_router(admin_identity_router.router)  # Who am I, OpenAPI
admin_api_router.include_router(admin_agent_router.router)  # Agents
admin_api_router.include_router(admin_alert_router.router)  # Alert rules
admin_api_router.include_router(
    admin_notification_provider_router.router
)  # Where alerts send
admin_api_router.include_router(
    admin_notification_log_router.router
)  # Every notification attempt
admin_api_router.include_router(admin_check_router.router)  # Checks
admin_api_router.include_router(
    admin_check_result_router.router
)  # Check results, history, summary
admin_api_router.include_router(
    admin_registration_key_router.router
)  # Agent registration keys
admin_api_router.include_router(admin_status_page_router.router)  # Status pages
admin_api_router.include_router(admin_setting_router.router)  # Settings
admin_api_router.include_router(
    admin_maintenance_job_router.router
)  # Background job status

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(agent_api_router)
api_router.include_router(admin_api_router)

__all__ = ["api_router"]
