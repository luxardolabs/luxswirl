"""The settings form's text is parsed to the setting's stored type."""

from __future__ import annotations

import pytest
from app.services.views.settings_view_service import SettingsViewService

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("value", "value_type", "parsed"),
    [("42", "int", 42), ("1.5", "float", 1.5), ("on", "bool", True), ("x", None, "x")],
)
def test_parse_value(value: str, value_type: str | None, parsed: object) -> None:
    assert SettingsViewService._parse_value(value, value_type) == parsed
