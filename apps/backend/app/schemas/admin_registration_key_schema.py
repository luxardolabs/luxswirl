"""
Pydantic schemas for agent registration keys on the admin API.

A key's plaintext is returned once, when it is created; only its hash is
stored, and no other response carries either.
"""

from pydantic import ConfigDict, Field

from app.schemas.paginated_response_schema import PaginatedResponse
from app.schemas.registration_key_schema import (
    RegistrationKeyCreate,
    RegistrationKeyResponse,
    RegistrationKeyRevoke,
)


class AdminRegistrationKeyCreate(RegistrationKeyCreate):
    """A new registration key: a name and optional notes."""

    model_config = ConfigDict(extra="forbid")

    description: str | None = Field(
        None, max_length=1000, description="Optional notes about this key"
    )


class AdminRegistrationKeyRevoke(RegistrationKeyRevoke):
    """Why the key is revoked; kept with the revocation."""

    model_config = ConfigDict(extra="forbid")


class RegistrationKeyCreated(RegistrationKeyResponse):
    """A key just created, with its plaintext: shown this once."""

    key: str = Field(..., description="The registration key, shown only now")


class RegistrationKeyPage(PaginatedResponse[RegistrationKeyResponse]):
    """One page of registration keys."""
