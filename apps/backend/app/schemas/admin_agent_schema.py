"""
Pydantic schemas for agents on the admin API.

An agent reports its own run id, address and version; the API changes only
what the web UI's edit form does, and refuses any other field (422).
"""

from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.agent_schema import AgentResponse, AgentUpdate
from app.schemas.paginated_response_schema import PaginatedResponse

# What the agent reports about itself, never set from outside.
_AGENT_REPORTED = ("agent_run_id", "ip_address", "version")


class AdminAgentUpdate(AgentUpdate):
    """A change to an agent: its name, tags and configuration, as the edit form."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "agent_name": "prod-web-01",
                "tags": ["production", "web"],
                "heartbeat_interval": 30,
                "log_level": "INFO",
            }
        },
    )

    @model_validator(mode="after")
    def _agent_reported_untouched(self) -> Self:
        reported = [f for f in _AGENT_REPORTED if f in self.model_fields_set]
        if reported:
            raise ValueError(
                f"These fields are reported by the agent: {', '.join(reported)}"
            )
        return self


class AdminAgentReason(BaseModel):
    """Why an agent is rejected, paused or disabled; shown with its status."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(None, max_length=500)


class AgentPage(PaginatedResponse[AgentResponse]):
    """One page of agents."""


class AgentKey(BaseModel):
    """An agent's new key, shown this once: only its hash is stored."""

    agent_id: UUID
    api_key: str
