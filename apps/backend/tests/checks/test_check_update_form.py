"""The edit form's translation into a partial CheckUpdate: a field the form did
not provide stays unset (the check keeps its value), and a submitted-but-empty
script or description clears that field."""

from __future__ import annotations

from uuid import uuid4

from app.services.views.checks_view_service import ChecksViewService


def test_nothing_provided_sets_nothing() -> None:
    update = ChecksViewService.transform_form_data_for_update()
    assert update.model_dump(exclude_unset=True) == {}


def test_only_provided_fields_are_set_and_parsed() -> None:
    parent = uuid4()
    update = ChecksViewService.transform_form_data_for_update(
        display_name="api",
        enabled=False,
        interval=30,
        tags="a, b,,",
        port="5353",
        timeout_seconds="7",
        retry_attempts="",
        agent_selector='{"region": "eu"}',
        depends_on_check_id=parent,
    )
    assert update.model_dump(exclude_unset=True) == {
        "display_name": "api",
        "enabled": False,
        "interval_seconds": 30,
        "tags": ["a", "b"],
        "port": 5353,
        "timeout_seconds": 7,
        "agent_selector": {"region": "eu"},
        "depends_on_check_id": parent,
    }


def test_empty_script_and_description_clear_the_field() -> None:
    update = ChecksViewService.transform_form_data_for_update(
        script_code="", description=""
    )
    assert update.model_dump(exclude_unset=True) == {
        "script_code": None,
        "description": None,
    }


def test_false_and_zero_like_values_are_still_provided() -> None:
    update = ChecksViewService.transform_form_data_for_update(
        enabled=False, verify_ssl=False
    )
    assert update.model_dump(exclude_unset=True) == {
        "enabled": False,
        "verify_ssl": False,
    }
