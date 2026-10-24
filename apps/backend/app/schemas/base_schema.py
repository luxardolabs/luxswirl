"""
Base Pydantic schemas with common configurations.
"""

import re
from datetime import datetime
from ipaddress import ip_address
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from app.core.datetime_utils import utc_now

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

MAX_TAGS = 100
MAX_TAG_LENGTH = 100


def upper_or_none(value: object) -> object:
    """Normalize an uppercase-valued enum input: ``""`` is unset, any other
    string is uppercased (``get`` -> ``GET``) before the enum validates it."""
    if isinstance(value, str):
        return value.strip().upper() or None
    return value


def refuse_control_chars(value: str) -> str:
    """Refuse a string holding a control character (a newline, NUL, ESC...).

    Stripping trims only the ends, so without this a value such as
    ``"web01\\nedge"`` is stored and shown on every page that names it.
    """
    if _CONTROL_CHARS.search(value):
        raise ValueError("must not contain control characters")
    return value


def canonical_ip(value: str) -> str:
    """An IPv4 or IPv6 address in its canonical text form; anything else is refused."""
    try:
        return str(ip_address(value))
    except ValueError:
        raise ValueError(f"not a valid IP address: {value!r}") from None


PlainText = Annotated[str, AfterValidator(refuse_control_chars)]
"""Free text an input sets on one line: a name, a hostname, a version."""

IpAddressText = Annotated[str, AfterValidator(canonical_ip)]

Tag = Annotated[
    str, Field(max_length=MAX_TAG_LENGTH), AfterValidator(refuse_control_chars)
]
TagList = Annotated[list[Tag], Field(max_length=MAX_TAGS)]
"""An input's tags: at most MAX_TAGS, each one line of at most MAX_TAG_LENGTH."""


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


class ErrorResponse(BaseSchema):
    """Schema for error responses."""

    error: str = Field(..., description="Error type or code")
    message: str = Field(..., description="Human-readable error message")
    details: dict[str, Any] | None = Field(None, description="Additional error details")
    timestamp: datetime = Field(
        default_factory=utc_now, description="When the error occurred (UTC)"
    )
