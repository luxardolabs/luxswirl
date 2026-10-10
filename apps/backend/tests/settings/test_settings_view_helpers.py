"""The settings side effects refuse a value of the wrong type instead of
passing it on: a display format must be text, a retention period whole days."""

from __future__ import annotations

import pytest
from app.core.exceptions import ValidationException
from app.services.views.settings_view_service import SettingsViewService

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("value", "value_type", "parsed"),
    [("42", "int", 42), ("1.5", "float", 1.5), ("on", "bool", True), ("x", None, "x")],
)
def test_parse_value(value: str, value_type: str | None, parsed: object) -> None:
    assert SettingsViewService._parse_value(value, value_type) == parsed


def test_a_display_setting_must_be_text() -> None:
    with pytest.raises(ValidationException):
        SettingsViewService._update_template_cache_if_needed("general.timezone", 5)


@pytest.mark.parametrize("value", ["90", 1.5, True])
async def test_a_retention_setting_must_be_whole_days(value: object) -> None:
    with pytest.raises(ValidationException):
        await SettingsViewService._update_timescale_policies_if_needed(
            None, "database.retention_days", value
        )


async def test_a_non_database_setting_is_left_alone() -> None:
    await SettingsViewService._update_timescale_policies_if_needed(
        None, "general.timezone", "UTC"
    )
