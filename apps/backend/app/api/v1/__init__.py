"""
API v1 router aggregation.

Collects all v1 routers and provides a single router for the main application.
"""

from fastapi import APIRouter

from app.api.v1.routers import (
    agent_router,
    alert_router,
    artifact_router,
    auth_router,
    check_router,
    import_export_router,
    job_router,
    notification_provider_router,
    registration_key_router,
    result_router,
    users_router,
)

# Create the main v1 API router
api_router = APIRouter(prefix="/api/v1")

# Include all routers
api_router.include_router(auth_router.router)  # Authentication
api_router.include_router(users_router.router)  # User management
api_router.include_router(agent_router.agent_ops_router)  # Agent operations (no prefix)
api_router.include_router(agent_router.router)  # Agent management (with /agents prefix)
api_router.include_router(check_router.router)  # Agent-facing check endpoints
api_router.include_router(check_router.management_router)  # Management check endpoints
api_router.include_router(result_router.router)
api_router.include_router(import_export_router.router)
api_router.include_router(notification_provider_router.router)
api_router.include_router(alert_router.router)
api_router.include_router(job_router.router)
api_router.include_router(artifact_router.router)
api_router.include_router(registration_key_router.router)

__all__ = ["api_router"]
