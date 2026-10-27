"""
Pydantic schemas for settings on the admin API.

A setting is read with its value and default unwrapped from their stored
form. A generated setting's value is a secret (the metrics bearer token): it
is read masked, and its plaintext is returned once, when it is generated.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.enum_model import SettingCategory, SettingValueType
from app.schemas.setting_schema import SettingScalar

# What a set secret reads as; an unset one reads as the empty string.
MASK = "********"


class AdminSetting(BaseModel):
    """One setting: its value, its default and the rules a new value meets."""

    key: str
    category: SettingCategory
    subcategory: str | None
    display_name: str
    description: str | None
    type: SettingValueType
    value: SettingScalar
    default_value: SettingScalar
    validation: dict[str, Any] | None
    generated: bool = Field(
        ..., description="Generated, never set to a value; read masked"
    )
    updated_at: datetime


class AdminSettingValue(BaseModel):
    """A new value, of the setting's type."""

    model_config = ConfigDict(extra="forbid")

    value: SettingScalar


class MetricsToken(BaseModel):
    """A new metrics bearer token, shown only now."""

    token: str
