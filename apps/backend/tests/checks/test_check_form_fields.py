"""The three check panels must keep their own required-field sets.

CheckCreate/Update/CloneFields share one base dataclass. A dataclass subclass
CANNOT make an inherited defaulted field required again — redeclaring it leaves
the base's default in place — so a field that must be required on one panel and
optional on another has to be declared per panel. Getting that wrong is silent:
the form still posts, FastAPI just stops rejecting the incomplete body, and the
handler runs on a half-filled form.
"""

from __future__ import annotations

import inspect

import pytest
from app.web.check_form import (
    CheckCloneFields,
    CheckCreateFields,
    CheckUpdateFields,
)

# What each panel genuinely cannot proceed without.
EXPECTED_REQUIRED = {
    CheckCreateFields: {"agent_id", "check_type"},
    CheckUpdateFields: {"check_type", "display_name", "target", "interval"},
    CheckCloneFields: {"agent_id", "check_type", "display_name", "target"},
}

# Every field a view service reads out of the form mapping.
SHARED_OPTIONAL = {
    "enabled",
    "tags",
    "http_method",
    "expected_status",
    "json_path",
    "expected_value",
    "script_code",
    "alert_ids",
    "assignment_mode",
    "agent_selector",
    "description",
    "timeout_seconds",
    "verify_ssl",
    "retry_attempts",
    "retry_interval_seconds",
    "resend_notification_after",
    "depends_on_check_id",
}


def _required(cls: type) -> set[str]:
    return {
        name
        for name, param in inspect.signature(cls).parameters.items()
        if param.default is inspect.Parameter.empty
    }


@pytest.mark.parametrize(
    ("cls", "expected"),
    EXPECTED_REQUIRED.items(),
    ids=lambda v: getattr(v, "__name__", ""),
)
def test_required_fields_are_declared_per_panel(cls: type, expected: set[str]) -> None:
    assert _required(cls) == expected


@pytest.mark.parametrize("cls", EXPECTED_REQUIRED, ids=lambda c: c.__name__)
def test_every_panel_carries_the_shared_optional_fields(cls: type) -> None:
    names = set(inspect.signature(cls).parameters)
    assert names >= SHARED_OPTIONAL


def test_as_dict_normalises_alert_ids_to_a_list() -> None:
    fields = CheckUpdateFields(
        check_type="ping", display_name="n", target="1.2.3.4", interval=60
    )
    assert fields.as_dict()["alert_ids"] == []
