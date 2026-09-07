"""
Maintenance job schemas.

The DTO the web seam holds for a backend-internal maintenance job. Every
surface that enqueues one (an agent delete, a bulk check action, an import)
embeds the same polling partial, so what the partial reads is the contract.

Columns only — no relationships — so validating one out of the session cannot
lazy-load.
"""

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models.enum_model import MaintenanceJobKind, MaintenanceJobStatus


class MaintenanceJobResponse(BaseModel):
    """A maintenance job as the polling partial and its callers read it."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    kind: MaintenanceJobKind
    target_id: UUID | None = None
    status: MaintenanceJobStatus
    progress: dict[str, Any] | None = None
    error: str | None = None
