"""The check-config packing, which three copies of a field list used to decide.

`create_check` and `update_check` each carried a verbatim copy of the
type→fields map, and `update_check` carried a third, hand-flattened copy. The
failure modes were silent in all three directions: miss the create copy and a
form value is never stored; miss the update copy and an edit drops it; miss the
flattened list and a non-column name reaches `setattr` and raises.

There is one map now. These tests hold the derivation and the mutation contract
that replaced the copies.
"""

from __future__ import annotations

import pytest
from app.core.check_config import (
    ALL_CONFIG_FIELDS,
    TYPE_FIELDS,
    pack_config,
    take_config_updates,
)

pytestmark = pytest.mark.unit


class _Data:
    """Stands in for a CheckCreate: attribute access, nothing else."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class TestTheFlatSetIsDerived:
    def test_it_is_exactly_the_union_of_the_map(self):
        """The thing the hand-written third copy could get wrong."""
        assert {
            f for fields in TYPE_FIELDS.values() for f in fields
        } == ALL_CONFIG_FIELDS

    def test_every_type_field_is_in_it(self):
        for check_type, fields in TYPE_FIELDS.items():
            for field in fields:
                assert field in ALL_CONFIG_FIELDS, (check_type, field)


class TestPackConfig:
    def test_it_stores_only_this_type_s_fields(self):
        data = _Data(
            http_method="GET",
            verify_ssl=True,
            expected_status=200,
            # belongs to dns, and must NOT be persisted against an http check
            record_type="A",
        )
        assert pack_config(data, "http") == {
            "http_method": "GET",
            "verify_ssl": True,
            "expected_status": 200,
        }

    def test_an_empty_string_is_unset_not_a_value(self):
        """The form posts "" for a field the operator left blank."""
        data = _Data(connection_string="", query="SELECT 1")
        assert pack_config(data, "postgres") == {"query": "SELECT 1"}

    def test_none_is_unset(self):
        data = _Data(connection_string=None, query="SELECT 1")
        assert pack_config(data, "postgres") == {"query": "SELECT 1"}

    def test_a_falsy_but_real_value_is_kept(self):
        """verify_ssl=False and port=0 are decisions, not blanks — an `if value`
        test would have dropped both."""
        data = _Data(http_method="GET", verify_ssl=False, expected_status=0)
        assert pack_config(data, "http") == {
            "http_method": "GET",
            "verify_ssl": False,
            "expected_status": 0,
        }

    def test_a_type_with_no_config_packs_nothing(self):
        assert pack_config(_Data(http_method="GET"), "ping") == {}

    def test_a_missing_attribute_is_not_an_error(self):
        """A create schema need not carry every type's fields."""
        assert pack_config(_Data(), "http") == {}


class TestTakeConfigUpdates:
    def test_it_returns_this_type_s_fields(self):
        update = {"display_name": "x", "http_method": "POST", "verify_ssl": False}
        assert take_config_updates(update, "http") == {
            "http_method": "POST",
            "verify_ssl": False,
        }

    def test_it_leaves_only_columns_behind(self):
        """THE contract. Whatever stays in the dict goes to setattr, so a config
        name surviving here is an AttributeError on a real update."""
        update = {
            "display_name": "x",
            "interval_seconds": 60,
            "http_method": "POST",
            # every other type's config fields, which this check type does not use
            "record_type": "A",
            "nameserver": "1.1.1.1",
            "connection_string": "postgresql://…",
            "query": "SELECT 1",
            "json_path": "$.a",
        }
        take_config_updates(update, "http")
        assert update == {"display_name": "x", "interval_seconds": 60}
        assert not (set(update) & ALL_CONFIG_FIELDS)

    def test_a_field_not_being_updated_is_absent_not_none(self):
        """exclude_unset means an untouched field is missing from the dict; it
        must not come back as None and blank the stored config."""
        update = {"http_method": "POST"}
        assert take_config_updates(update, "http") == {"http_method": "POST"}

    def test_an_empty_string_clears_nothing_and_is_dropped(self):
        update = {"http_method": "", "verify_ssl": True}
        assert take_config_updates(update, "http") == {"verify_ssl": True}
        assert "http_method" not in update

    def test_a_type_with_no_config_still_strips_every_config_name(self):
        update = {"display_name": "x", "query": "SELECT 1"}
        assert take_config_updates(update, "ping") == {}
        assert update == {"display_name": "x"}
