"""Unit tests for app/core/authorization/scopes.py: LuxPM's own, ported with LuxSwirl's families."""

import pytest
from app.core.authorization.scopes import (
    ACTIONS,
    DEFAULT_SCOPES,
    FAMILIES,
    expand_wildcards,
    parse_scope_string,
    scope_grants,
    validate_scope,
)


class TestParseScopeString:
    def test_concrete(self):
        assert parse_scope_string("checks:read") == ("checks", "read")

    def test_family_wildcard(self):
        assert parse_scope_string("checks:*") == ("checks", "*")

    def test_action_wildcard(self):
        assert parse_scope_string("*:read") == ("*", "read")

    def test_full_wildcard(self):
        assert parse_scope_string("*") == ("*", "*")

    @pytest.mark.parametrize(
        "bad", ["", "checks", "checks:", ":read", "checks:read:extra", "Checks:Read"]
    )
    def test_malformed_raises(self, bad):
        with pytest.raises(ValueError):
            parse_scope_string(bad)


class TestValidateScope:
    @pytest.mark.parametrize(
        "scope",
        ["checks:read", "agents:write", "alerts:admin", "checks:*", "*:read", "*"],
    )
    def test_valid(self, scope):
        assert validate_scope(scope) is True

    @pytest.mark.parametrize(
        "scope",
        [
            "unknown:read",
            "checks:execute",
            "",
            "checks",
            "Checks:Read",
            ":read",
            "*:foo",
        ],
    )
    def test_invalid(self, scope):
        assert validate_scope(scope) is False


class TestExpandWildcards:
    def test_empty(self):
        assert expand_wildcards([]) == frozenset()

    def test_concrete_only(self):
        assert expand_wildcards(["checks:read"]) == frozenset({"checks:read"})

    def test_family_wildcard_expands_to_all_actions(self):
        result = expand_wildcards(["checks:*"])
        expected = {f"checks:{a}" for a in ACTIONS}
        assert result == frozenset(expected)

    def test_action_wildcard_expands_to_all_families(self):
        result = expand_wildcards(["*:read"])
        expected = {f"{f}:read" for f in FAMILIES}
        assert result == frozenset(expected)

    def test_full_wildcard_expands_to_full_grid(self):
        result = expand_wildcards(["*"])
        expected = {f"{f}:{a}" for f in FAMILIES for a in ACTIONS}
        assert result == frozenset(expected)

    def test_dedup_across_overlapping_grants(self):
        result = expand_wildcards(["checks:*", "checks:read", "*:read"])
        # checks:read appears via three paths, must dedup
        assert "checks:read" in result
        assert "checks:write" in result
        assert "agents:read" in result
        assert sum(1 for s in result if s == "checks:read") == 1

    def test_invalid_scope_silently_dropped(self):
        # Malformed entries don't raise -- they just contribute nothing.
        result = expand_wildcards(["checks:read", "garbage", "unknown:foo"])
        assert result == frozenset({"checks:read"})


class TestScopeGrants:
    def test_exact_match(self):
        assert scope_grants(["checks:read"], "checks:read") is True

    def test_no_match(self):
        assert scope_grants(["checks:read"], "checks:write") is False

    def test_family_wildcard_grants_action(self):
        assert scope_grants(["checks:*"], "checks:write") is True
        assert scope_grants(["checks:*"], "checks:admin") is True

    def test_family_wildcard_does_not_cross_families(self):
        assert scope_grants(["checks:*"], "agents:read") is False

    def test_action_wildcard_grants_across_families(self):
        assert scope_grants(["*:read"], "checks:read") is True
        assert scope_grants(["*:read"], "agents:read") is True

    def test_action_wildcard_does_not_cross_actions(self):
        assert scope_grants(["*:read"], "checks:write") is False

    def test_full_wildcard_grants_everything(self):
        assert scope_grants(["*"], "alerts:admin") is True
        assert scope_grants(["*"], "jobs:write") is True

    def test_default_scopes_apply_when_empty(self):
        # Empty/None scope set falls back to DEFAULT_SCOPES (*:read).
        assert scope_grants([], "checks:read") is True
        assert scope_grants(None, "agents:read") is True
        assert scope_grants([], "checks:write") is False

    def test_multiple_grants_unioned(self):
        scopes = ["checks:write", "agents:read", "jobs:*"]
        assert scope_grants(scopes, "checks:write") is True
        assert scope_grants(scopes, "agents:read") is True
        assert scope_grants(scopes, "jobs:admin") is True
        assert scope_grants(scopes, "agents:write") is False

    def test_required_must_be_concrete(self):
        # Asking "does this token grant a wildcard?" is nonsense; treat as False.
        assert scope_grants(["*"], "*") is False
        assert scope_grants(["*"], "checks:*") is False
        assert scope_grants(["*"], "*:read") is False

    def test_required_invalid_returns_false(self):
        assert scope_grants(["*"], "garbage") is False
        assert scope_grants(["*"], "unknown:read") is False

    def test_invalid_grant_does_not_break_valid_ones(self):
        scopes = ["garbage", "checks:read"]
        assert scope_grants(scopes, "checks:read") is True
        assert scope_grants(scopes, "checks:write") is False


class TestDefaults:
    def test_default_is_read_only(self):
        for family in FAMILIES:
            assert scope_grants(DEFAULT_SCOPES, f"{family}:read") is True
            assert scope_grants(DEFAULT_SCOPES, f"{family}:write") is False
            assert scope_grants(DEFAULT_SCOPES, f"{family}:admin") is False
