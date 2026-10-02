"""Which check-type fields live in `check_config`, and the packing both ways.

A pure domain contract — no session, no I/O — so it sits in `app/core/`
alongside `synthetic_security` and `check_target_validator`. It is PUBLIC, not a
`_`-helper of the core service: the mapping IS the check-config contract, and it
has focused tests of its own, which is the signal that it is a capability rather
than one service's implementation detail.

It exists because
`create_check` and `update_check` each carried their own verbatim copy of the
type→fields map, and `update_check` carried a THIRD, hand-flattened copy as its
`all_config_fields` list. Add a field to the `http` check type and you had to
remember all three: miss the create copy and the UI silently drops the value;
miss the update copy and an edit silently drops it; miss the flattened list and
a non-column field reaches `setattr` and raises.

There is one map now, and the flat set is derived from it rather than written
out again.
"""

from __future__ import annotations

from typing import Any

from app.schemas.check_schema import CheckCreate

# The check-type-specific fields that are packed into the `check_config` JSONB
# rather than stored as columns. A check type absent from this map has no
# type-specific config.
TYPE_FIELDS: dict[str, tuple[str, ...]] = {
    "http": ("http_method", "verify_ssl", "expected_status"),
    "json": (
        "http_method",
        "verify_ssl",
        "expected_status",
        "json_path",
        "expected_value",
    ),
    "dns": ("record_type", "nameserver", "port", "expect_value"),
    "mysql": ("connection_string", "query"),
    "postgres": ("connection_string", "query"),
}

# DERIVED, never hand-written: every name that belongs in check_config for any
# check type. update_check uses it to strip config keys out of the update dict
# before the remainder reaches setattr — a name missing from here would be set
# as a model attribute that does not exist.
ALL_CONFIG_FIELDS: frozenset[str] = frozenset(
    field for fields in TYPE_FIELDS.values() for field in fields
)


def _is_set(value: object) -> bool:
    """Whether a value is worth storing. An empty string is the form's "unset".

    `object`, not `Any`: this compares and never dereferences, so it genuinely
    takes any config scalar — while `Any` would blind mypy at a boundary that
    has a real type.
    """
    return value is not None and value != ""


def pack_config(data: CheckCreate, check_type: str) -> dict[str, Any]:
    """The `check_config` for a NEW check, from a create schema.

    Reads only the fields that belong to `check_type`, so a value left over
    from another type's form cannot be persisted against this one.
    """
    return {
        field: getattr(data, field)
        for field in TYPE_FIELDS.get(check_type, ())
        if _is_set(getattr(data, field, None))
    }


def take_config_updates(update_data: dict[str, Any], check_type: str) -> dict[str, Any]:
    """POP this check type's config fields out of an update dict and return them.

    Mutates `update_data`: every config name is removed — the ones for this
    check type into the returned dict, the rest dropped — so what remains is
    only direct model fields and is safe to `setattr`.
    """
    updates = {
        field: value
        for field in TYPE_FIELDS.get(check_type, ())
        if field in update_data and _is_set(value := update_data.pop(field))
    }
    for field in ALL_CONFIG_FIELDS:
        update_data.pop(field, None)
    return updates
