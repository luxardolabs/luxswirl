"""
Pydantic schemas for Registration Key domain.
"""

from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.base import BaseSchema, TimestampSchema


class RegistrationKeyCreate(BaseSchema):
    """Schema for creating a new registration key."""

    name: str = Field(..., min_length=1, max_length=255, description="Name for this key")
    description: str | None = Field(None, description="Optional notes about this key")

    model_config = {
        "json_schema_extra": {
            "example": {
                "name": "Production Agents",
                "description": "Registration key for production environment agents",
            }
        }
    }


class RegistrationKeyCreateResponse(BaseSchema):
    """Response when creating a registration key (includes plaintext key ONCE)."""

    id: UUID = Field(..., description="Key UUID")
    name: str = Field(..., description="Key name")
    key: str = Field(..., description="Plaintext key (shown only once!)")
    message: str = Field(
        default="Save this key securely - it cannot be retrieved again",
        description="Warning message",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "550e8400-e29b-41d4-a716-446655440000",
                "name": "Production Agents",
                "key": "luxswirl_rk_abcdef1234567890abcdef1234567890",
                "message": "Save this key securely - it cannot be retrieved again",
            }
        }
    }


class RegistrationKeyUpdate(BaseSchema):
    """Schema for updating a registration key."""

    name: str | None = Field(None, min_length=1, max_length=255, description="New name")
    description: str | None = Field(None, description="New description")

    model_config = {
        "json_schema_extra": {
            "example": {
                "name": "Production Agents (Updated)",
                "description": "Updated description",
            }
        }
    }


class RegistrationKeyRevoke(BaseSchema):
    """Schema for revoking a registration key."""

    reason: str | None = Field(None, max_length=500, description="Reason for revocation")

    model_config = {
        "json_schema_extra": {
            "example": {
                "reason": "Key compromised - rotating to new key",
            }
        }
    }


class RegistrationKeyResponse(TimestampSchema):
    """Schema for registration key response (never includes plaintext key)."""

    id: UUID = Field(..., description="Key UUID")
    name: str = Field(..., description="Key name")
    description: str | None = Field(None, description="Key description")
    created_by: str | None = Field(None, description="Admin who created key")
    last_used_at: datetime | None = Field(None, description="Last usage timestamp")
    usage_count: int = Field(..., description="Number of times used")
    revoked_at: datetime | None = Field(None, description="Revocation timestamp")
    revoked_by: str | None = Field(None, description="Admin who revoked key")
    revoked_reason: str | None = Field(None, description="Revocation reason")
    status: str = Field(..., description="Key status (active/revoked)")

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "550e8400-e29b-41d4-a716-446655440000",
                "name": "Production Agents",
                "description": "Registration key for production environment",
                "created_by": None,
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
                "last_used_at": "2024-01-15T14:30:00Z",
                "usage_count": 42,
                "revoked_at": None,
                "revoked_by": None,
                "revoked_reason": None,
                "status": "active",
            }
        }
    }


class RegistrationKeyListResponse(BaseSchema):
    """Response containing list of registration keys."""

    keys: list[RegistrationKeyResponse] = Field(..., description="List of registration keys")
    total: int = Field(..., description="Total number of keys")

    model_config = {
        "json_schema_extra": {
            "example": {
                "keys": [
                    {
                        "id": "550e8400-e29b-41d4-a716-446655440000",
                        "name": "Production Agents",
                        "status": "active",
                        "usage_count": 42,
                        "created_at": "2024-01-01T00:00:00Z",
                    }
                ],
                "total": 1,
            }
        }
    }


class AgentKeyRecoveryRequest(BaseSchema):
    """Request to recover agent-specific key using registration token."""

    agent_id: UUID = Field(..., description="Agent UUID requesting recovery")

    model_config = {
        "json_schema_extra": {
            "example": {
                "agent_id": "550e8400-e29b-41d4-a716-446655440000",
            }
        }
    }


class AgentKeyRecoveryResponse(BaseSchema):
    """Response for agent key recovery (includes new plaintext key)."""

    agent_id: UUID = Field(..., description="Agent UUID")
    api_key: str = Field(..., description="New agent-specific API key")
    message: str = Field(..., description="Recovery message")

    model_config = {
        "json_schema_extra": {
            "example": {
                "agent_id": "550e8400-e29b-41d4-a716-446655440000",
                "api_key": "luxswirl_ak_abcdef1234567890abcdef1234567890",
                "message": "Agent key recovered successfully. Save this key securely.",
            }
        }
    }


class AgentKeyRegenerateResponse(BaseSchema):
    """Response when admin regenerates agent key."""

    agent_id: UUID = Field(..., description="Agent UUID")
    api_key: str = Field(..., description="New agent-specific API key")
    message: str = Field(
        default="Key regenerated - update agent configuration with new key",
        description="Instructions for admin",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "agent_id": "550e8400-e29b-41d4-a716-446655440000",
                "api_key": "luxswirl_ak_abcdef1234567890abcdef1234567890",
                "message": "Key regenerated - update agent configuration with new key",
            }
        }
    }
