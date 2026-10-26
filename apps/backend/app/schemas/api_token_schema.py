"""
Pydantic schemas for the API token domain.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import Field, field_validator

from app.core.datetime_utils import utc_now
from app.models.enum_model import UserRole
from app.schemas.auth_schema import CurrentUserDTO
from app.schemas.base_schema import BaseSchema, TimestampSchema


class ApiTokenCreate(BaseSchema):
    """A new API token: what it is for, and how long it lasts."""

    name: str = Field(..., min_length=1, max_length=100, description="What it is for")
    expires_in_days: int | None = Field(
        None, ge=1, le=3650, description="Days until it stops working; None = never"
    )

    @field_validator("name")
    @classmethod
    def _printable(cls, name: str) -> str:
        # A NUL is refused by Postgres mid-insert; the rest would garble the list.
        if any(ord(c) < 32 or ord(c) == 127 for c in name):
            raise ValueError("A token name cannot contain control characters")
        return name


class ApiTokenResponse(TimestampSchema):
    """An API token as its owner sees it: never the token itself."""

    id: UUID = Field(..., description="Token UUID")
    name: str = Field(..., description="What it is for")
    token_prefix: str = Field(..., description="The token's first characters")
    last_used_at: datetime | None = Field(None, description="Last authenticated")
    expires_at: datetime | None = Field(None, description="Stops working at")
    revoked_at: datetime | None = Field(None, description="Revoked at")

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= utc_now()


@dataclass(frozen=True)
class ApiPrincipal:
    """Who an admin-API request acts as: the token's user, and the token."""

    user: CurrentUserDTO
    token_id: UUID
    token_name: str
    token_expires_at: datetime | None


class ApiWhoAmIResponse(BaseSchema):
    """The user and token behind the calling credential."""

    user_id: UUID = Field(..., description="The token's user")
    username: str = Field(..., description="The user's login name")
    role: UserRole = Field(..., description="The user's role, which the token has")
    token_id: UUID = Field(..., description="The calling token")
    token_name: str = Field(..., description="The calling token's name")
    token_expires_at: datetime | None = Field(
        None, description="When the calling token stops working"
    )
