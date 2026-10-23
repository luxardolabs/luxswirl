"""
API v1 router aggregation.

The JSON API is the agent protocol: registration, key recovery, heartbeats,
the agent's own check list, result reports, job results and artifact uploads.
"""

from fastapi import APIRouter

from app.api.v1.routers import (
    agent_router,
    artifact_router,
    check_router,
    job_router,
    result_router,
)

# Create the main v1 API router
api_router = APIRouter(prefix="/api/v1")

api_router.include_router(
    agent_router.agent_ops_router
)  # Register, heartbeat, key recovery
api_router.include_router(check_router.router)  # The agent's own check list
api_router.include_router(result_router.router)  # Result reports
api_router.include_router(job_router.router)  # Job results
api_router.include_router(artifact_router.router)  # Artifact uploads

__all__ = ["api_router"]
