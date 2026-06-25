"""Pure-logic tests for each check type's `validate_config`.

These tests don't execute checks against real targets — they only validate
that the config gate rejects bad input. Network-bound `run()` testing belongs
in a separate `_run_*` test file with explicit mocking of the network layer.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.checks.dns import DNSCheck
from app.checks.http import HTTPCheck
from app.checks.json import JSONCheck
from app.checks.mysql import MySQLCheck
from app.checks.ping import PingCheck
from app.checks.postgres import PostgreSQLCheck
from app.checks.synthetic import SyntheticCheck
from app.checks.tcp import TCPCheck

pytestmark = pytest.mark.pure


def _base_cfg(**overrides):
    cfg = {
        "check_id": str(uuid4()),
        "name": "test-check",
        "check_type": "ping",
        "target": "127.0.0.1",
    }
    cfg.update(overrides)
    return cfg


# ---------------------------------------------------------------------------
# BaseCheck — required fields
# ---------------------------------------------------------------------------


class TestBaseValidation:
    def test_missing_check_id_raises(self):
        cfg = _base_cfg()
        del cfg["check_id"]
        with pytest.raises(ValueError, match="check_id"):
            PingCheck(cfg)

    def test_missing_target_raises(self):
        cfg = _base_cfg()
        del cfg["target"]
        with pytest.raises(ValueError, match="target"):
            PingCheck(cfg)

    def test_missing_check_type_raises(self):
        cfg = _base_cfg()
        del cfg["check_type"]
        with pytest.raises(ValueError, match="check_type"):
            PingCheck(cfg)


# ---------------------------------------------------------------------------
# PingCheck
# ---------------------------------------------------------------------------


class TestPingValidation:
    def test_valid_config_accepts(self):
        PingCheck(_base_cfg(check_type="ping", target="8.8.8.8"))

    def test_url_target_rejected(self):
        """Ping targets are hostnames or IPs, not URLs."""
        with pytest.raises(ValueError, match="should not include protocol"):
            PingCheck(_base_cfg(check_type="ping", target="https://example.test"))


# ---------------------------------------------------------------------------
# HTTPCheck
# ---------------------------------------------------------------------------


class TestHTTPValidation:
    def test_valid_https_target_accepts(self):
        HTTPCheck(_base_cfg(check_type="http", target="https://example.test/health"))

    def test_valid_http_target_accepts(self):
        HTTPCheck(_base_cfg(check_type="http", target="http://example.test/health"))

    def test_target_without_protocol_rejected(self):
        with pytest.raises(ValueError, match="http:// or https://"):
            HTTPCheck(_base_cfg(check_type="http", target="example.test/health"))

    def test_ftp_target_rejected(self):
        with pytest.raises(ValueError, match="http:// or https://"):
            HTTPCheck(_base_cfg(check_type="http", target="ftp://example.test"))


# ---------------------------------------------------------------------------
# TCPCheck
# ---------------------------------------------------------------------------


class TestTCPValidation:
    def test_valid_port_accepts(self):
        TCPCheck(_base_cfg(check_type="tcp", target="example.test", port=443))

    def test_missing_port_rejected(self):
        with pytest.raises(ValueError, match="port"):
            TCPCheck(_base_cfg(check_type="tcp", target="example.test"))

    def test_port_zero_rejected(self):
        with pytest.raises(ValueError, match="between 1 and 65535"):
            TCPCheck(_base_cfg(check_type="tcp", target="example.test", port=0))

    def test_port_too_high_rejected(self):
        with pytest.raises(ValueError, match="between 1 and 65535"):
            TCPCheck(_base_cfg(check_type="tcp", target="example.test", port=70000))

    def test_port_negative_rejected(self):
        with pytest.raises(ValueError, match="between 1 and 65535"):
            TCPCheck(_base_cfg(check_type="tcp", target="example.test", port=-1))

    def test_port_string_rejected(self):
        with pytest.raises(ValueError, match="integer"):
            TCPCheck(_base_cfg(check_type="tcp", target="example.test", port="443"))


# ---------------------------------------------------------------------------
# DNSCheck
# ---------------------------------------------------------------------------


class TestDNSValidation:
    def test_valid_a_record(self):
        DNSCheck(_base_cfg(check_type="dns", target="example.test", record_type="A"))

    def test_valid_cname_record(self):
        DNSCheck(_base_cfg(check_type="dns", target="example.test", record_type="CNAME"))

    def test_missing_record_type_rejected(self):
        with pytest.raises(ValueError, match="record_type"):
            DNSCheck(_base_cfg(check_type="dns", target="example.test"))

    def test_invalid_record_type_rejected(self):
        with pytest.raises(ValueError, match="Invalid DNS record type"):
            DNSCheck(
                _base_cfg(
                    check_type="dns",
                    target="example.test",
                    record_type="QUUX",
                )
            )

    def test_lowercase_record_type_normalized(self):
        """The validator uppercases the record_type before checking — so
        lowercase input is accepted."""
        DNSCheck(_base_cfg(check_type="dns", target="example.test", record_type="a"))


# ---------------------------------------------------------------------------
# JSONCheck (HTTP-based, JSONata query)
# ---------------------------------------------------------------------------


class TestJSONValidation:
    def test_valid_config(self):
        JSONCheck(
            _base_cfg(
                check_type="json",
                target="https://api.example.test/health",
                json_path="$.status",
                expected_value="ok",
            )
        )

    def test_http_target_required(self):
        with pytest.raises(ValueError, match="http:// or https://"):
            JSONCheck(
                _base_cfg(
                    check_type="json",
                    target="not-a-url",
                    json_path="$.status",
                    expected_value="ok",
                )
            )

    def test_missing_json_path_rejected(self):
        with pytest.raises(ValueError, match="json_path"):
            JSONCheck(
                _base_cfg(
                    check_type="json",
                    target="https://api.example.test/health",
                    expected_value="ok",
                )
            )

    def test_missing_expected_value_rejected(self):
        with pytest.raises(ValueError, match="expected_value"):
            JSONCheck(
                _base_cfg(
                    check_type="json",
                    target="https://api.example.test/health",
                    json_path="$.status",
                )
            )


# ---------------------------------------------------------------------------
# MySQLCheck (connection_string OR target works; query optional)
# ---------------------------------------------------------------------------


class TestMySQLValidation:
    def test_valid_with_target_connection_string(self):
        MySQLCheck(
            _base_cfg(
                check_type="mysql",
                target="mysql://user:pass@db.example.test:3306/mydb",
            )
        )

    def test_valid_with_explicit_connection_string(self):
        MySQLCheck(
            _base_cfg(
                check_type="mysql",
                target="placeholder",  # base requires non-empty target
                connection_string="mariadb://user:pass@db:3306/mydb",
            )
        )

    def test_invalid_scheme_rejected(self):
        with pytest.raises(ValueError, match="Invalid MySQL connection string scheme"):
            MySQLCheck(
                _base_cfg(
                    check_type="mysql",
                    target="postgresql://user:pass@db:5432/mydb",
                )
            )


# ---------------------------------------------------------------------------
# PostgreSQLCheck
# ---------------------------------------------------------------------------


class TestPostgresValidation:
    def test_valid_config(self):
        PostgreSQLCheck(
            _base_cfg(
                check_type="postgres",
                target="postgresql://user:pass@db.example.test:5432/mydb",
            )
        )

    def test_invalid_scheme_rejected(self):
        with pytest.raises(ValueError):
            PostgreSQLCheck(
                _base_cfg(
                    check_type="postgres",
                    target="mysql://user:pass@db:3306/mydb",
                )
            )


# ---------------------------------------------------------------------------
# SyntheticCheck — script_code presence only (AST validation lives in backend)
# ---------------------------------------------------------------------------


class TestSyntheticValidation:
    def test_script_code_required(self):
        SyntheticCheck(
            _base_cfg(
                check_type="synthetic",
                target="https://example.test",
                script_code="async def run_check(page):\n    return {}\n",
            )
        )

    def test_missing_script_rejected(self):
        with pytest.raises(ValueError, match="script_code"):
            SyntheticCheck(_base_cfg(check_type="synthetic", target="https://example.test"))

    def test_empty_script_rejected(self):
        with pytest.raises(ValueError, match="script_code"):
            SyntheticCheck(
                _base_cfg(
                    check_type="synthetic",
                    target="https://example.test",
                    script_code="",
                )
            )

    def test_dangerous_script_accepted_at_agent_level(self):
        """The AGENT's SyntheticCheck only enforces presence — AST security
        gating lives in the backend's CheckCoreService.create_check (see
        tests/checks/test_check_service_integration.py). This documents the
        boundary: the agent trusts that the backend has already validated
        the script before sending it down."""
        SyntheticCheck(
            _base_cfg(
                check_type="synthetic",
                target="https://example.test",
                script_code="import os\nos.system('whoami')\n",
            )
        )
