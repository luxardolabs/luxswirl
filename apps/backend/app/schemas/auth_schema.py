"""
Authentication schemas - Pydantic models for auth requests/responses.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LoginRequest(BaseModel):
    """Request schema for user login."""

    username: str = Field(..., min_length=1, max_length=100, description="Username")
    password: str = Field(..., min_length=1, description="Password")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "username": "admin",
                "password": "Admin123",
            }
        }
    )


class LoginResponse(BaseModel):
    """Response schema for successful login."""

    message: str = Field(..., description="Success message")
    user: UserResponse = Field(..., description="Logged in user details")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "message": "Login successful",
                "user": {
                    "id": "550e8400-e29b-41d4-a716-446655440000",
                    "username": "admin",
                    "role": "admin",
                    "full_name": "System Administrator",
                    "is_active": True,
                    "must_change_password": False,
                    "last_login_at": "2025-01-08T12:00:00Z",
                    "created_at": "2025-01-01T00:00:00Z",
                },
            }
        }
    )


class ChangePasswordRequest(BaseModel):
    """Request schema for password change."""

    current_password: str = Field(..., min_length=1, description="Current password")
    new_password: str = Field(
        ...,
        min_length=8,
        max_length=100,
        description="New password (min 8 characters)",
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

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "current_password": "OldPassword123",
                "new_password": "NewPassword123",
            }
        }
    )


class UserResponse(BaseModel):
    """Response schema for user data (excluding sensitive fields)."""

    id: UUID
    username: str
    role: str
    full_name: str | None = None
    is_active: bool
    must_change_password: bool
    last_login_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SessionResponse(BaseModel):
    """Response schema for session data."""

    id: UUID
    user_id: UUID
    created_at: datetime
    expires_at: datetime
    last_activity_at: datetime | None = None
    ip_address: str | None = None
    user_agent: str | None = None

    model_config = ConfigDict(from_attributes=True)


class SessionListResponse(BaseModel):
    """Response schema for user's session list."""

    sessions: list[SessionResponse]
    total: int
