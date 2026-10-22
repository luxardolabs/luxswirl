"""A check's HTTP method and DNS record type read back as their enums.

Both live in the check's JSON config. They were read as plain strings, so a
stored value outside the set reached the response schema and failed there (a
500 on the check's API read) instead of reading as unset.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from app.models.check_model import Check
from app.models.enum_model import DnsRecordType, HttpMethod

pytestmark = pytest.mark.unit


def _check(**config: object) -> Check:
    return Check(
        id=uuid4(),
        agent_id=uuid4(),
        display_name="c",
        check_type="http",
        target="https://example.test",
        check_config=config,
    )


def test_a_stored_method_reads_as_its_enum() -> None:
    method = _check(http_method="POST").http_method
    assert method is HttpMethod.POST


def test_a_stored_record_type_reads_as_its_enum() -> None:
    record = _check(record_type="AAAA").record_type
    assert record is DnsRecordType.AAAA


def test_a_lower_case_value_reads_as_the_schemas_would() -> None:
    check = _check(http_method=" post", record_type="aaaa")
    assert check.http_method is HttpMethod.POST
    assert check.record_type is DnsRecordType.AAAA


@pytest.mark.parametrize("stored", ["BREW", "", 7, None])
def test_a_value_outside_the_set_reads_as_unset(stored: object) -> None:
    check = _check(http_method=stored, record_type=stored)
    assert check.http_method is None
    assert check.record_type is None
