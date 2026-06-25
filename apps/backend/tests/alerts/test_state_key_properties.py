"""Property-based tests for AlertCoreService._compute_alert_state_key.

Hypothesis tests assert invariants the function must hold for ALL inputs
in a domain, not just the cases we thought to write down. These are
impossible to fabricate — they either find a real counterexample or they
don't. Part of the anti-fabrication doctrine (see tests/README.md).

Invariants verified:
- Determinism: same input → same key, always
- SSL band monotonicity: as days_until_expiration decreases, the key
  must escalate (or hold) — it must never relax to a wider band
- SSL threshold-order invariance: the order of `days_thresholds` in
  config must not affect the resulting key (sorted internally)
- Parent-down trumps everything: parent_down=True wins for any trigger
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.services.core.alert_core_service import AlertCoreService

pytestmark = pytest.mark.pure


def _alert(trigger_type: str, config: dict | None = None):
    return SimpleNamespace(trigger_type=trigger_type, trigger_config=config or {})


def _ssl_result(days_until: int):
    r = SimpleNamespace()
    r.get_metrics = lambda: {"response": {"ssl_certificate": {"days_until_expiration": days_until}}}
    return r


def _status_result(success: bool):
    return SimpleNamespace(success=success)


# ---------------------------------------------------------------------------
# Determinism — same input must produce same key
# ---------------------------------------------------------------------------


@given(success=st.booleans())
def test_status_change_is_deterministic(success):
    alert = _alert("status_change")
    result = _status_result(success)
    assert AlertCoreService._compute_alert_state_key(
        alert, result
    ) == AlertCoreService._compute_alert_state_key(alert, result)


@given(
    metric=st.sampled_from(["latency_ms", "http_status_code", "response_size"]),
    operator=st.sampled_from([">", ">=", "<", "<=", "=="]),
    value=st.integers(min_value=1, max_value=10000),
)
def test_threshold_key_is_deterministic(metric, operator, value):
    alert = _alert("threshold", {"metric": metric, "operator": operator, "value": value})
    result = _status_result(False)
    k1 = AlertCoreService._compute_alert_state_key(alert, result)
    k2 = AlertCoreService._compute_alert_state_key(alert, result)
    assert k1 == k2


@given(days_until=st.integers(min_value=-3650, max_value=3650))
def test_ssl_key_is_deterministic(days_until):
    alert = _alert("ssl_cert_expiry", {"days_thresholds": [7, 14, 30]})
    r = _ssl_result(days_until)
    assert AlertCoreService._compute_alert_state_key(
        alert, r
    ) == AlertCoreService._compute_alert_state_key(alert, r)


# ---------------------------------------------------------------------------
# SSL band monotonicity — tightening days must tighten (or hold) the band
# ---------------------------------------------------------------------------


def _ssl_severity(key: str) -> int:
    """Order SSL keys by severity. Lower = safer, higher = more urgent."""
    if key == "ssl:ok":
        return 0
    if key == "ssl:unknown":
        return -1  # uncomparable, but we filter this out before comparing
    if key.startswith("ssl:lte:"):
        # Tighter band = lower threshold number = higher severity
        # ssl:lte:30 → severity 1, ssl:lte:14 → severity 2, ssl:lte:7 → severity 3
        threshold = int(key.split(":")[-1])
        # Use a fixed inversion: smaller threshold = larger severity score
        return 1000 - threshold
    return -1


@given(
    days_a=st.integers(min_value=-365, max_value=365),
    days_b=st.integers(min_value=-365, max_value=365),
)
def test_ssl_band_monotonic_in_days(days_a, days_b):
    """For any two day values, the one with fewer days remaining must produce
    a key of equal or greater severity (never less severe).

    In plain terms: a cert with 5 days left must not produce a wider band key
    than the same cert with 30 days left.
    """
    alert = _alert("ssl_cert_expiry", {"days_thresholds": [7, 14, 30]})
    key_a = AlertCoreService._compute_alert_state_key(alert, _ssl_result(days_a))
    key_b = AlertCoreService._compute_alert_state_key(alert, _ssl_result(days_b))
    sev_a = _ssl_severity(key_a)
    sev_b = _ssl_severity(key_b)
    # Skip unknown (negative-1 sentinel) — that's not a band-comparison case
    if sev_a < 0 or sev_b < 0:
        return
    if days_a < days_b:
        assert sev_a >= sev_b, (
            f"days_a={days_a} (key={key_a}, sev={sev_a}) has fewer days than "
            f"days_b={days_b} (key={key_b}, sev={sev_b}), but lower severity"
        )
    elif days_b < days_a:
        assert sev_b >= sev_a, (
            f"days_b={days_b} (key={key_b}, sev={sev_b}) has fewer days than "
            f"days_a={days_a} (key={key_a}, sev={sev_a}), but lower severity"
        )


@given(days_until=st.integers(min_value=-1000, max_value=1000))
def test_ssl_key_is_always_in_known_set(days_until):
    """The SSL key must always be one of the documented values, never
    something exotic the dedup logic doesn't know how to compare."""
    alert = _alert("ssl_cert_expiry", {"days_thresholds": [7, 14, 30]})
    key = AlertCoreService._compute_alert_state_key(alert, _ssl_result(days_until))
    assert key in {"ssl:ok", "ssl:lte:7", "ssl:lte:14", "ssl:lte:30"}


# ---------------------------------------------------------------------------
# Threshold-order invariance — config order must not change the result
# ---------------------------------------------------------------------------


@given(
    thresholds=st.lists(
        st.integers(min_value=1, max_value=365),
        min_size=1,
        max_size=8,
        unique=True,
    ),
    days_until=st.integers(min_value=-100, max_value=400),
)
def test_ssl_threshold_order_invariant(thresholds, days_until):
    """The user can configure thresholds in any order; the resulting key
    must be identical to a sorted-thresholds configuration."""
    sorted_alert = _alert("ssl_cert_expiry", {"days_thresholds": sorted(thresholds)})
    reversed_alert = _alert(
        "ssl_cert_expiry", {"days_thresholds": sorted(thresholds, reverse=True)}
    )
    r = _ssl_result(days_until)
    assert AlertCoreService._compute_alert_state_key(
        sorted_alert, r
    ) == AlertCoreService._compute_alert_state_key(reversed_alert, r)


# ---------------------------------------------------------------------------
# Parent-down dominates trigger type
# ---------------------------------------------------------------------------


@given(
    trigger=st.sampled_from(
        [
            "status_change",
            "threshold",
            "repeated_failure",
            "ssl_cert_expiry",
            "some_unknown_future_type",
        ]
    ),
    success=st.booleans(),
)
def test_parent_down_overrides_all_triggers(trigger, success):
    """Whenever parent_down=True, the key MUST be 'parent_down' regardless
    of trigger type or check result state. This is what makes parent
    suppression dedup correctly across mixed alert configurations."""
    alert = _alert(trigger)
    result = _status_result(success)
    key = AlertCoreService._compute_alert_state_key(alert, result, parent_down=True)
    assert key == "parent_down"


# ---------------------------------------------------------------------------
# Status-change distinguishability — up and down must never collide
# ---------------------------------------------------------------------------


def test_status_up_and_down_are_distinct():
    """Trivial but load-bearing: if status:up == status:down then dedup
    can't tell a recovery from a continued outage. Pinned forever."""
    alert = _alert("status_change")
    up = AlertCoreService._compute_alert_state_key(alert, _status_result(True))
    down = AlertCoreService._compute_alert_state_key(alert, _status_result(False))
    assert up != down
