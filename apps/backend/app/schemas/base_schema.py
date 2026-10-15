"""
Base Pydantic schemas with common configurations.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.datetime_utils import utc_now


def upper_or_none(value: object) -> object:
    """Normalize an uppercase-valued enum input: ``""`` is unset, any other
    string is uppercased (``get`` -> ``GET``) before the enum validates it."""
    if isinstance(value, str):
        return value.strip().upper() or None
    return value


class BaseSchema(BaseModel):
    """Base schema with common configuration."""

    model_config = ConfigDict(
        from_attributes=True,  # Allow creation from ORM models
        populate_by_name=True,  # Allow population by field name or alias
        str_strip_whitespace=True,  # Strip whitespace from strings
        json_schema_extra={"example": {}},  # Subclasses should override
    )


class TimestampSchema(BaseSchema):
    """Schema with timestamp fields."""

    created_at: datetime = Field(..., description="When the record was created")
    updated_at: datetime = Field(..., description="When the record was last updated")


class ResponseSchema(BaseSchema):
    """Base schema for API responses."""

    success: bool = Field(..., description="Whether the request was successful")
    message: str | None = Field(None, description="Optional message")
    data: Any = Field(None, description="Response data")


class MessageResponse(BaseSchema):
    """A bare acknowledgement: the operation succeeded, here is what happened.

    Used by routes whose only payload is a human-readable confirmation, so the
    shape is still a real contract in openapi.json rather than an untyped dict.
    """

    message: str = Field(..., description="Human-readable result message")


class SessionsTerminatedResponse(MessageResponse):
    """Acknowledgement for a bulk logout, with how many sessions were ended."""

    sessions_terminated: int = Field(
        ..., ge=0, description="Number of sessions that were terminated"
    )


class MappingCreatedResponse(MessageResponse):
    """Acknowledgement for creating a join-table mapping, with its id."""

    mapping_id: UUID = Field(..., description="Id of the mapping that was created")


class ErrorResponse(BaseSchema):
    """Schema for error responses."""

    error: str = Field(..., description="Error type or code")
    message: str = Field(..., description="Human-readable error message")
    details: dict[str, Any] | None = Field(None, description="Additional error details")
    timestamp: datetime = Field(
        default_factory=utc_now, description="When the error occurred (UTC)"
    )
