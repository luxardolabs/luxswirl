"""
Base Pydantic schemas with common configurations.
"""

import unicodedata
from datetime import datetime
from ipaddress import ip_address
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
)

from app.core.datetime_utils import utc_now
from app.core.storable_text import storable_text

# Control (C0, DEL, C1), format (bidi overrides, zero-width), and line and
# paragraph separators: each one breaks a line or changes how text around it reads.
# A lone surrogate (a JSON "\ud800") is text UTF-8 cannot encode: no page, column
# or log line can hold it.
_REFUSED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})

MAX_TAGS = 100
MAX_TAG_LENGTH = 100


def upper_or_none(value: object) -> object:
    """Normalize an uppercase-valued enum input: ``""`` is unset, any other
    string is uppercased (``get`` -> ``GET``) before the enum validates it."""
    if isinstance(value, str):
        return value.strip().upper() or None
    return value


def refuse_control_chars(value: str) -> str:
    """Refuse a string holding a control character (a newline, NUL, ESC, NEL,
    a line separator, a bidi override...).

    Stripping trims only the ends, so without this a value such as
    ``"web01\\nedge"`` is stored and shown on every page that names it.
    """
    if any(unicodedata.category(c) in _REFUSED_CATEGORIES for c in value):
        raise ValueError("must not contain control characters")
    return value


# What a note that spans lines may hold of the control characters.
_NOTE_WHITESPACE = frozenset("\n\r\t")


def refuse_control_chars_in_notes(value: str) -> str:
    """Refuse a control character other than a line break or tab (a NUL, ESC,
    a bidi override...) in text that may span lines."""
    if any(
        c not in _NOTE_WHITESPACE and unicodedata.category(c) in _REFUSED_CATEGORIES
        for c in value
    ):
        raise ValueError("must not contain control characters")
    return value


def blank_to_none(value: object) -> object:
    """An empty or all-space string is unset."""
    if isinstance(value, str) and not value.strip():
        return None
    return value


def canonical_ip(value: str | None) -> str | None:
    """An IPv4 or IPv6 address in its canonical text form; anything else is refused."""
    if value is None:
        return None
    try:
        return str(ip_address(value))
    except ValueError:
        # The refused text is not repeated: the message reaches the UI's toast.
        raise ValueError("not a valid IP address") from None


def _refuse_unstorable(value: object) -> None:
    if isinstance(value, str):
        if not storable_text(value):
            # The refused text is not repeated: no response or log line can hold it.
            raise ValueError("must hold only text UTF-8 can encode, with no NUL")
    elif isinstance(value, dict):
        for key, item in value.items():
            _refuse_unstorable(key)
            _refuse_unstorable(item)
    elif isinstance(value, list):
        for item in value:
            _refuse_unstorable(item)


def storable_json[T](value: T) -> T:
    """Refuse a JSON value holding, at any depth, a key or string no column can
    store (`app.core.storable_text`)."""
    _refuse_unstorable(value)
    return value


PlainText = Annotated[str, AfterValidator(refuse_control_chars)]
"""Free text an input sets on one line: a name, a hostname, a version."""

NoteText = Annotated[str, AfterValidator(refuse_control_chars_in_notes)]
"""Free text that may span lines: a description, a reason."""

StorableJson = Annotated[dict[str, Any], AfterValidator(storable_json)]
"""A free-form JSON object an input sets: settings, metrics, parameters."""

IpAddressText = Annotated[
    str | None, BeforeValidator(blank_to_none), AfterValidator(canonical_ip)
]
"""An optional IP address: blank is unset, anything else must parse."""

Tag = Annotated[
    str, Field(max_length=MAX_TAG_LENGTH), AfterValidator(refuse_control_chars)
]
TagList = Annotated[list[Tag], Field(max_length=MAX_TAGS)]
"""An input's tags: at most MAX_TAGS, each one line of at most MAX_TAG_LENGTH."""

_TAG_LIST = TypeAdapter(TagList)


def storable_tags(tags: list[str]) -> list[str] | None:
    """`tags` as a TagList stores them, or None when they do not fit one."""
    try:
        return _TAG_LIST.validate_python(tags)
    except ValidationError:
        return None


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
