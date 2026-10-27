"""
Pydantic schemas for users on the admin API.

A user is never read with their password or its hash. A field the API does
not take is refused (422), never silently dropped.
"""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.base_schema import PlainText
from app.schemas.paginated_response_schema import PaginatedResponse
from app.schemas.user_schema import UserCreate, UserResponse, UserUpdate

# Fields every user has: a change may set them, never clear them.
_REQUIRED = ("role", "is_active", "must_change_password")


class AdminUserCreate(UserCreate):
    """A new user, who must change the password at first sign-in unless told
    otherwise."""

    model_config = ConfigDict(extra="forbid")

    full_name: PlainText | None = Field(None, max_length=255)
    must_change_password: bool = True


class AdminUserUpdate(UserUpdate):
    """A change to a user: the fields sent; a field left out is unchanged, and
    `full_name` sent as null is cleared."""

    model_config = ConfigDict(extra="forbid")

    full_name: PlainText | None = Field(None, max_length=255)

    @model_validator(mode="after")
    def _required_stay_set(self) -> Self:
        cleared = [
            f
            for f in _REQUIRED
            if f in self.model_fields_set and getattr(self, f) is None
        ]
        if cleared:
            raise ValueError(f"These fields cannot be null: {', '.join(cleared)}")
        return self


class AdminPasswordReset(BaseModel):
    """A new password the admin sets; the user's sessions and API tokens end."""

    model_config = ConfigDict(extra="forbid")

    new_password: str = Field(..., min_length=8, max_length=100)
    must_change: bool = Field(
        True, description="The user must change it at next sign-in"
    )


class UserPage(PaginatedResponse[UserResponse]):
    """One page of users."""
