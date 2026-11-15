"""Synthetic browser timings are shaped for the panel, not in it.

The agent reports raw performance.timing marks and per-step durations in
seconds; the panel subtracted marks and multiplied seconds inline, and looked
each step's duration out of a second dict by loop variable.
"""

from __future__ import annotations

import pytest
from app.core.synthetic_timings import build_synthetic_view


def test_no_synthetic_block_is_zeroed():
    view = build_synthetic_view(None)
    assert view == {
        "synthetic_total_ms": 0,
        "synthetic_steps": [],
        "synthetic_page": [
            {"label": "DOM Load (ms)", "mark": "domLoading", "ms": None},
            {"label": "DOM Complete (ms)", "mark": "domComplete", "ms": None},
            {"label": "Full Load (ms)", "mark": "loadEventEnd", "ms": None},
        ],
        "synthetic_execution_ms": None,
    }


def test_the_page_marks_become_ms_since_navigation_started():
    view = build_synthetic_view(
        {
            "browser_timing": {
                "navigationStart": 1_000,
                "domLoading": 1_120,
                "domComplete": 1_400,
                "loadEventEnd": None,
            },
            "total_execution_time_ms": 2400.7,
        }
    )
    assert [tile["ms"] for tile in view["synthetic_page"]] == [120, 400, None]
    assert view["synthetic_execution_ms"] == 2400


@pytest.mark.parametrize(
    "synthetic",
    [
        ["status", "failure"],
        {"track_durations": ["a"], "track_timings": "x"},
        {"track_durations": {"a": "1.5", "b": None, "c": True}},
        {"track_durations": {"a": 1e308, "b": float("nan")}},
        {"track_timings": {"a": "x", "b": [1], "c": {"navigationStart": "1"}}},
        {"browser_timing": {"navigationStart": None, "domLoading": 1200}},
        {"browser_timing": {"navigationStart": 1, "domLoading": 10**400}},
        {"total_execution_time_ms": float("inf")},
    ],
)
def test_a_figure_that_is_no_finite_number_is_none_never_an_error(synthetic):
    view = build_synthetic_view(synthetic)

    assert [tile["ms"] for tile in view["synthetic_page"]] == [None, None, None]
    assert view["synthetic_execution_ms"] is None
    assert view["synthetic_total_ms"] == 0
    for step in view["synthetic_steps"]:
        assert step["dom_load_ms"] is None


def test_total_is_the_sum_of_step_durations_in_ms():
    view = build_synthetic_view({"track_durations": {"a": 1.5, "b": 0.25}})
    assert view["synthetic_total_ms"] == 1750


def test_marks_become_ms_since_that_step_navigation_started():
    view = build_synthetic_view(
        {
            "track_durations": {"login": 2.0},
            "track_timings": {
                "login": {
                    "navigationStart": 1_000_000,
                    "domLoading": 1_000_120,
                    "domComplete": 1_000_400,
                    "loadEventEnd": 1_000_950,
                }
            },
        }
    )
    step = view["synthetic_steps"][0]
    assert step["name"] == "login"
    assert step["dom_load_ms"] == 120
    assert step["dom_complete_ms"] == 400
    assert step["full_load_ms"] == 950
    assert step["total_ms"] == 2000


def test_each_step_uses_its_own_navigation_start():
    """Two steps in one run start at different marks; neither may borrow the other's."""
    view = build_synthetic_view(
        {
            "track_durations": {"one": 1.0, "two": 3.0},
            "track_timings": {
                "one": {"navigationStart": 100, "loadEventEnd": 150},
                "two": {"navigationStart": 500, "loadEventEnd": 590},
            },
        }
    )
    by_name = {s["name"]: s for s in view["synthetic_steps"]}
    assert by_name["one"]["full_load_ms"] == 50
    assert by_name["two"]["full_load_ms"] == 90


def test_a_missing_mark_is_none_not_a_bogus_number():
    view = build_synthetic_view(
        {"track_timings": {"s": {"navigationStart": 100, "loadEventEnd": 180}}}
    )
    step = view["synthetic_steps"][0]
    assert step["full_load_ms"] == 80
    assert step["dom_load_ms"] is None
    assert step["dom_complete_ms"] is None


def test_a_step_with_no_recorded_duration_reports_zero():
    view = build_synthetic_view(
        {"track_durations": {}, "track_timings": {"s": {"navigationStart": 0}}}
    )
    assert view["synthetic_steps"][0]["total_ms"] == 0
