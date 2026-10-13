"""Closed-set fields refuse values outside their set at the boundary.

`fw.enum_dto_fields_enum_typed`. The artifact content type is the one with teeth:
the console serves an artifact inline with the type the agent uploaded, so an
open set let an agent key upload `text/html` and run script on the console's
origin. It is now only the types the agent's synthetic check produces.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from app.core.datetime_utils import utc_now
from app.models.enum_model import (
    ArtifactContentType,
    RegistrationKeyStatus,
    SettingValueType,
)
from app.models.registration_key_model import RegistrationKey
from app.models.setting_model import Setting
from app.schemas.check_artifact_schema import CheckArtifactCreate
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def _artifact(**overrides) -> CheckArtifactCreate:
    fields = {
        "check_id": uuid4(),
        "check_result_id": uuid4(),
        "check_result_timestamp": utc_now(),
        "artifact_type": "screenshot",
        "content_type": "image/png",
        "filename": "home_screenshot.png",
        "data_base64": base64.b64encode(b"\x89PNG").decode(),
    } | overrides
    return CheckArtifactCreate(**fields)


@pytest.mark.parametrize(
    "content_type", ["text/html", "image/svg+xml", "application/xhtml+xml", ""]
)
def test_an_artifact_that_could_render_as_a_page_is_refused(content_type: str) -> None:
    with pytest.raises(ValidationError):
        _artifact(content_type=content_type)


def test_the_agents_artifact_types_are_accepted() -> None:
    assert _artifact().content_type is ArtifactContentType.PNG
    trace = _artifact(
        artifact_type="trace", content_type="application/zip", filename="t.zip"
    )
    assert trace.content_type is ArtifactContentType.ZIP


def test_an_unknown_artifact_type_is_refused() -> None:
    with pytest.raises(ValidationError):
        _artifact(artifact_type="pdf")


@pytest.mark.parametrize(
    ("value", "tag"),
    [
        (True, SettingValueType.BOOL),
        (60, SettingValueType.INT),
        (0.5, SettingValueType.FLOAT),
        ("GET", SettingValueType.STRING),
        ([22, 80], SettingValueType.LIST),
        ({"a": 1}, SettingValueType.DICT),
        (datetime(2026, 1, 1, tzinfo=UTC), SettingValueType.STRING),
    ],
)
def test_a_setting_value_is_tagged_with_its_type(value, tag) -> None:
    setting = Setting()
    setting.set_typed_value(value)
    assert setting.value == {"value": value, "type": tag}


def test_a_registration_key_is_active_until_revoked() -> None:
    key = RegistrationKey()
    assert key.status is RegistrationKeyStatus.ACTIVE
    key.revoked_at = utc_now()
    assert key.status is RegistrationKeyStatus.REVOKED
