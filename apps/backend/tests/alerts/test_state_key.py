"""Unit tests for AlertCoreService._compute_alert_state_key.

The state key is the dedup primitive — it must be stable for the same logical
state and distinct across transitions. See LUXSWIRL-147.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.core.alert_core_service import AlertCoreService

pytestmark = pytest.mark.pure  # all tests in this module are pure-logic


def _alert(trigger_type: str, config: dict | None = None):
    return SimpleNamespace(trigger_type=trigger_type, trigger_config=config or {})


def _result(success: bool = True, metrics: dict | None = None):
    r = SimpleNamespace(success=success)
    r.get_metrics = lambda: metrics or {}
    return r


def _ssl_result(days_until: int | None):
    cert = {} if days_until is None else {"days_until_expiration": days_until}
    return _result(success=True, metrics={"response": {"ssl_certificate": cert}})


class TestStatusChange:
    def test_down(self):
        key = AlertCoreService._compute_alert_state_key(
            _alert("status_change"), _result(success=False)
        )
        assert key == "status:down"

    def test_up(self):
        key = AlertCoreService._compute_alert_state_key(
            _alert("status_change"), _result(success=True)
        )
        assert key == "status:up"


class TestThreshold:
    def test_includes_metric_op_value(self):
        alert = _alert("threshold", {"metric": "latency_ms", "operator": ">", "value": 5000})
        assert (
            AlertCoreService._compute_alert_state_key(alert, _result())
            == "threshold:latency_ms:>:5000"
        )

    def test_uses_defaults_when_unconfigured(self):
        # operator defaults to ">", metric defaults to latency_ms
        alert = _alert("threshold", {"value": 1000})
        assert (
            AlertCoreService._compute_alert_state_key(alert, _result())
            == "threshold:latency_ms:>:1000"
        )


class TestRepeatedFailure:
    def test_active(self):
        assert (
            AlertCoreService._compute_alert_state_key(
                _alert("repeated_failure"), _result(success=False)
            )
            == "repeated_failure:active"
        )


class TestSSLCertExpiry:
    """Multi-band escalation — the critical LUXSWIRL-146 fix."""

    @pytest.fixture
    def alert(self):
        return _alert("ssl_cert_expiry", {"days_thresholds": [7, 14, 30]})

    def test_safe(self, alert):
        assert AlertCoreService._compute_alert_state_key(alert, _ssl_result(60)) == "ssl:ok"

    def test_in_30_day_band(self, alert):
        assert AlertCoreService._compute_alert_state_key(alert, _ssl_result(25)) == "ssl:lte:30"

    def test_in_14_day_band(self, alert):
        assert AlertCoreService._compute_alert_state_key(alert, _ssl_result(12)) == "ssl:lte:14"

    def test_in_7_day_band(self, alert):
        assert AlertCoreService._compute_alert_state_key(alert, _ssl_result(5)) == "ssl:lte:7"

    def test_expired_uses_tightest_band(self, alert):
        # Negative days (already expired) still classified into the tightest band
        assert AlertCoreService._compute_alert_state_key(alert, _ssl_result(-252)) == "ssl:lte:7"

    def test_exactly_at_boundary_uses_inclusive_band(self, alert):
        # 14 days exactly → in the 14-day band (tightest <=)
        assert AlertCoreService._compute_alert_state_key(alert, _ssl_result(14)) == "ssl:lte:14"
        assert AlertCoreService._compute_alert_state_key(alert, _ssl_result(7)) == "ssl:lte:7"

    def test_missing_cert_data(self, alert):
        # No SSL cert at all in metrics
        no_cert = _result(metrics={"response": {}})
        assert AlertCoreService._compute_alert_state_key(alert, no_cert) == "ssl:unknown"

    def test_missing_days_until(self, alert):
        assert AlertCoreService._compute_alert_state_key(alert, _ssl_result(None)) == "ssl:unknown"

    def test_legacy_single_threshold(self):
        # Falls back to days_threshold (singleton) when days_thresholds is empty
        alert = _alert("ssl_cert_expiry", {"days_threshold": 30})
        assert AlertCoreService._compute_alert_state_key(alert, _ssl_result(25)) == "ssl:lte:30"


class TestParentDown:
    def test_overrides_trigger_type(self):
        # parent_down=True short-circuits regardless of trigger_type
        for trigger in ("status_change", "threshold", "ssl_cert_expiry"):
            assert (
                AlertCoreService._compute_alert_state_key(
                    _alert(trigger), _result(), parent_down=True
                )
                == "parent_down"
            )


class TestUnknownTrigger:
    def test_falls_through_to_active(self):
        # Unknown trigger types still produce a key so dedup doesn't crash
        assert (
            AlertCoreService._compute_alert_state_key(_alert("some_future_type"), _result())
            == "some_future_type:active"
        )


class TestStability:
    """Same logical state must produce the same key across calls."""

    def test_same_state_same_key(self):
        alert = _alert("ssl_cert_expiry", {"days_thresholds": [7, 14, 30]})
        result = _ssl_result(12)
        key1 = AlertCoreService._compute_alert_state_key(alert, result)
        key2 = AlertCoreService._compute_alert_state_key(alert, result)
        assert key1 == key2 == "ssl:lte:14"

    def test_threshold_order_independent(self):
        # User can configure thresholds in any order — same key result
        sorted_alert = _alert("ssl_cert_expiry", {"days_thresholds": [7, 14, 30]})
        unsorted_alert = _alert("ssl_cert_expiry", {"days_thresholds": [30, 7, 14]})
        for days in (5, 12, 25, 60):
            assert AlertCoreService._compute_alert_state_key(
                sorted_alert, _ssl_result(days)
            ) == AlertCoreService._compute_alert_state_key(unsorted_alert, _ssl_result(days))
