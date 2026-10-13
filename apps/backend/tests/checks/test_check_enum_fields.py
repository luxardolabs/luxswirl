"""Closed-set check fields are enum-typed at the schema boundary.

HTTP method and DNS record type are validated against their enums
(`fw.enum_dto_fields_enum_typed`). The agent has always accepted any case and
the forms post an empty value for a field the check type does not use, so the
boundary uppercases and treats "" as unset, and refuses anything outside the set.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from app.models.enum_model import (
    BulkCheckOutcome,
    CheckHealthStatus,
    CheckType,
    DnsRecordType,
    HttpMethod,
)
from app.schemas.check_schema import (
    BulkCheckResult,
    CheckCreate,
    CheckUpdate,
)
from app.services.views.status_view_service import StatusRow
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def _create(**fields) -> CheckCreate:
    return CheckCreate(
        display_name="enum-fields",
        check_type=CheckType.HTTP,
        target="https://x.test",
        **fields,
    )


@pytest.mark.parametrize("sent", ["GET", "get", " Get "])
def test_any_case_http_method_is_its_enum(sent: str) -> None:
    assert _create(http_method=sent).http_method is HttpMethod.GET


@pytest.mark.parametrize("sent", ["aaaa", "AAAA"])
def test_any_case_record_type_is_its_enum(sent: str) -> None:
    assert CheckUpdate(record_type=sent).record_type is DnsRecordType.AAAA


@pytest.mark.parametrize("field", ["http_method", "record_type"])
def test_an_empty_form_value_is_unset(field: str) -> None:
    assert getattr(_create(**{field: ""}), field) is None


@pytest.mark.parametrize(
    ("field", "sent"), [("http_method", "FETCH"), ("record_type", "AXFR")]
)
def test_a_value_outside_the_set_is_refused(field: str, sent: str) -> None:
    with pytest.raises(ValidationError):
        _create(**{field: sent})


def test_a_bulk_outcome_outside_the_set_is_refused() -> None:
    with pytest.raises(ValidationError):
        BulkCheckResult(url="https://x.test", status="created")
    assert (
        BulkCheckResult(url="https://x.test", status="failed").status
        is BulkCheckOutcome.FAILED
    )


def _row(success: bool | None) -> StatusRow:
    return StatusRow(
        check_id=uuid4(),
        agent_id=uuid4(),
        check_name="c",
        check_type=CheckType.PING,
        target="127.0.0.1",
        agent_name="a",
        success=success,
        latency_ms=None,
        last_check=None,
        uptime_24h=None,
        enabled=True,
        fully_qualified_name="a:c",
    )


@pytest.mark.parametrize(
    ("success", "health"),
    [
        (True, CheckHealthStatus.UP),
        (False, CheckHealthStatus.DOWN),
        (None, CheckHealthStatus.UNKNOWN),
    ],
)
def test_a_dashboard_row_reports_its_health(
    success: bool | None, health: CheckHealthStatus
) -> None:
    assert _row(success).health is health
