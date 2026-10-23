"""
CheckArtifact Pydantic schemas for validation and serialization.
"""

import base64
from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.models.enum_model import ArtifactContentType, CheckArtifactType
from app.schemas.base_schema import BaseSchema


class CheckArtifactCreate(BaseSchema):
    """
    Schema for creating a check artifact.
    Used by agents to submit screenshots, traces, etc.

    Binary data is sent as base64-encoded string.
    """

    agent_id: UUID = Field(
        ..., description="The uploading agent (its own key must authenticate it)"
    )
    check_id: UUID = Field(..., description="Check UUID this artifact belongs to")
    check_result_id: UUID = Field(..., description="Check result UUID (from agent)")
    check_result_timestamp: datetime = Field(
        ..., description="Check result timestamp (from agent)"
    )
    artifact_type: CheckArtifactType = Field(..., description="Type of artifact")
    content_type: ArtifactContentType = Field(..., description="MIME type")
    filename: str = Field(
        ..., description="Original filename for download", max_length=255
    )
    data_base64: str = Field(..., description="Binary artifact data (base64 encoded)")

    def get_binary_data(self) -> bytes:
        """Decode base64 data to bytes."""
        return base64.b64decode(self.data_base64)

    model_config = {
        "json_schema_extra": {
            "example": {
                "agent_id": "550e8400-e29b-41d4-a716-446655440000",
                "check_id": "123e4567-e89b-12d3-a456-426614174000",
                "check_result_id": "9b2f6c1e-3d4a-4f5b-8c7d-0e1f2a3b4c5d",
                "check_result_timestamp": "2026-10-22T10:00:00Z",
                "artifact_type": "screenshot",
                "content_type": "image/png",
                "filename": "screenshot.png",
                "data_base64": "<base64 encoded binary data>",
            }
        }
    }


class CheckArtifactResponse(BaseSchema):
    """
    Schema for artifact metadata (without binary data).
    Used for listing artifacts.
    """

    id: UUID = Field(..., description="Artifact UUID")
    check_id: UUID = Field(..., description="Check UUID")
    check_result_id: UUID = Field(..., description="Check result UUID")
    check_result_timestamp: datetime = Field(..., description="Check result timestamp")
    artifact_type: CheckArtifactType = Field(..., description="Type of artifact")
    content_type: ArtifactContentType = Field(..., description="MIME type")
    filename: str = Field(..., description="Original filename")
    size_bytes: int = Field(..., description="Size of binary data in bytes")
    created_at: datetime = Field(..., description="When artifact was created")

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "987fcdeb-51a2-43c1-b456-426614174111",
                "check_id": "123e4567-e89b-12d3-a456-426614174000",
                "check_result_id": 42,
                "artifact_type": "screenshot",
                "content_type": "image/png",
                "filename": "check_result_42.png",
                "size_bytes": 153600,
                "created_at": "2024-10-22T15:30:00Z",
            }
        }
    }


class CheckArtifactContent(CheckArtifactResponse):
    """An artifact's metadata plus its bytes, for serving a download."""

    data: bytes = Field(..., description="Binary artifact data")
