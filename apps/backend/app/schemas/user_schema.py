"""
User schemas - Pydantic models for user management.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enum_model import UserRole


class UserCreate(BaseModel):
    """Request schema for creating a new user."""

    username: str = Field(..., min_length=3, max_length=100, description="Username (unique)")
    password: str = Field(
        ..., min_length=8, max_length=100, description="Password (min 8 characters)"
    )
    role: UserRole = Field(UserRole.VIEWER, description="User role: admin, editor, viewer")
    full_name: str | None = Field(None, max_length=255, description="Full name")
    is_active: bool = Field(True, description="Whether user is active")
    must_change_password: bool = Field(False, description="Force password change on first login")

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        """Validate username format."""
        if not v.replace("_", "").replace("-", "").replace(".", "").isalnum():
            raise ValueError(
                "Username can only contain letters, numbers, underscores, hyphens, and periods"
            )
        return v

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, v: str) -> str:
        """Validate password meets minimum strength requirements."""
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters long")
        if not any(c.isupper() for c in v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not any(c.islower() for c in v):
            raise ValueError("Password must contain at least one lowercase letter")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one digit")
        return v

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "username": "johndoe",
                "password": "SecurePass123",
                "role": "editor",
                "full_name": "John Doe",
                "is_active": True,
                "must_change_password": False,
            }
        }
    )


class UserUpdate(BaseModel):
    """Request schema for updating a user."""

    role: UserRole | None = Field(None, description="User role")
    full_name: str | None = Field(None, max_length=255, description="Full name")
    is_active: bool | None = Field(None, description="Whether user is active")
    must_change_password: bool | None = Field(None, description="Force password change")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "role": "admin",
                "full_name": "John Smith",
                "is_active": True,
            }
        }
    )


class UserPasswordReset(BaseModel):
    """Request schema for admin resetting user password."""

    new_password: str = Field(..., min_length=8, max_length=100, description="New password")
    must_change_password: bool = Field(
        True, description="Require user to change password on next login"
    )

    @field_validator("new_password")
    @classmethod
    def validate_password_strength(cls, v: str) -> str:
        """Validate password meets minimum strength requirements."""
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters long")
        if not any(c.isupper() for c in v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not any(c.islower() for c in v):
            raise ValueError("Password must contain at least one lowercase letter")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one digit")
        return v


class UserResponse(BaseModel):
    """Response schema for user data."""

    id: UUID
    username: str
    role: str
    full_name: str | None = None
    is_active: bool
    must_change_password: bool
    last_login_at: datetime | None = None
    password_changed_at: datetime | None = None
    failed_login_attempts: int
    locked_until: datetime | None = None
    created_by: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class UserListResponse(BaseModel):
    """Response schema for paginated user list."""

    users: list[UserResponse]
    total: int
    page: int = 1
    page_size: int = 50


class UserStatsResponse(BaseModel):
    """Response schema for user statistics."""

    total_users: int
    active_users: int
    admin_count: int
    editor_count: int
    viewer_count: int
    locked_accounts: int
