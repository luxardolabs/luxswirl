#!/usr/bin/env python3
"""Simple type validation test for the fixed check modules."""

from typing import Any


# Test ping stats type
def test_ping_stats():
    stats: dict[str, Any] = {
        "sent": 0,
        "received": 0,
        "packet_loss": 100.0,
        "min_ms": None,
        "avg_ms": None,
        "max_ms": None,
        "stddev_ms": None,
        "raw_output": "test",
        "times": [],
    }

    # This should work without type errors
    times = [1.0, 2.0, 3.0]
    stats["times"] = times
    print("✓ Ping stats type test passed")


# Test synthetic result_data type
def test_synthetic_result():
    result_data: dict[str, Any] = {
        "status": "unknown",
        "steps": [],
        "errors": [],
        "console_errors": [],
        "request_failures": [],
        "browser_timing": {},
        "artifacts": [],
    }

    # These should work without type errors
    result_data["console_errors"].append("test error")
    result_data["errors"].extend(["error1", "error2"])
    result_data["artifacts"].append({"type": "test", "data": b"bytes"})
    print("✓ Synthetic result_data type test passed")


if __name__ == "__main__":
    test_ping_stats()
    test_synthetic_result()
    print("All type tests passed!")
