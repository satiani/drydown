"""Tests for the pure calibration math: slopes, watering detection, and the
top-level per-plant computation. No I/O is involved here."""

from __future__ import annotations

import datetime as dt

import pytest

import conftest  # noqa: F401  (installs the appdaemon stub at import time)
import calibration as cal


# ---- helpers ---------------------------------------------------------------

def row(t, mn, mx, mean, last=None):
    """Build a daily-aggregate row like the InfluxDB layer produces."""
    return {"t": t, "mn": mn, "mx": mx, "mean": mean,
            "last": mean if last is None else last}


def dates(start="2024-01-01", n=10):
    d = dt.date.fromisoformat(start)
    return [(d + dt.timedelta(days=i)).isoformat() for i in range(n)]


def compute(mrows, cur_mo, crows=None, **cfg_over):
    """Run compute_plant, stamping cur_mo onto the final moisture row's `last`
    (matching how the InfluxDB layer + last_value work)."""
    config = cal.CalibrationConfig(**cfg_over)
    if mrows:
        mrows[-1] = dict(mrows[-1], last=cur_mo)
    return cal.compute_plant("p", "sensor.p_moisture",
                             mrows, crows or [], config)


# ---- last_value ------------------------------------------------------------

def test_last_value_takes_final_non_null_last():
    rows = [{"t": "2024-01-01", "last": 18}, {"t": "2024-01-02", "last": 22}]
    assert cal.last_value(rows) == 22


def test_last_value_skips_trailing_null_last():
    assert cal.last_value([{"t": "d1", "last": 30},
                           {"t": "d2", "last": None}]) == 30
    assert cal.last_value([{"t": "d1", "last": None}]) is None
    assert cal.last_value([]) is None


# ---- linear slope ----------------------------------------------------------

def test_linear_slope_descending():
    assert cal.linear_slope([10, 8, 6, 4, 2]) == pytest.approx(-2.0)


def test_linear_slope_flat():
    assert cal.linear_slope([5, 5, 5]) == pytest.approx(0.0)


def test_linear_slope_degenerate():
    assert cal.linear_slope([]) is None
    assert cal.linear_slope([3]) is None
    assert cal.linear_slope([3, 3]) == pytest.approx(0.0)


# ---- slope_over ------------------------------------------------------------

def test_slope_requires_three_points():
    rows = [row(t, 0, 0, 10) for t in dates(n=2)]
    assert cal.slope_over(rows, n=7) is None
    rows = [row(t, 0, 0, m) for t, m in zip(dates(n=3), [10, 8, 6])]
    assert cal.slope_over(rows, n=7) == pytest.approx(-2.0)


def test_slope_uses_real_dates_when_days_skipped():
    """3 reading-days spanning 8 calendar days: 30 -> 26 -> 22 = -1 %/day.
    Integer-index regression would wrongly report -4 %/day."""
    rows = [row("2024-01-01", 0, 0, 30),
            row("2024-01-05", 0, 0, 26),
            row("2024-01-09", 0, 0, 22)]
    assert cal.slope_over(rows, n=3) == pytest.approx(-1.0)


def test_slope_consecutive_dates_matches_integer_regression():
    rows = [row(t, 0, 0, m) for t, m in zip(dates(n=3), [10, 8, 6])]
    assert cal.slope_over(rows, n=3) == pytest.approx(-2.0)


# ---- detect_waterings ------------------------------------------------------

def test_detect_watering_jump():
    means = [20, 19, 18, 17, 45, 43, 41, 39, 37]
    mins = [m - 1 for m in means]
    maxs = [m + 1 for m in means]
    maxs[4] = 48  # the watering day spikes in the max
    mrows = [row(t, mn, mx, mean)
             for t, mn, mx, mean in zip(dates(n=len(means)), mins, maxs, means)]
    events = cal.detect_waterings(mrows, jump_thresh=8)
    assert len(events) == 1
    assert events[0].pre_min == mins[3]
    assert events[0].plateau >= 41


def test_detect_no_watering_when_steady():
    means = [30, 30, 30, 30, 30]
    mrows = [row(t, m - 1, m + 1, m) for t, m in zip(dates(n=5), means)]
    assert cal.detect_waterings(mrows, jump_thresh=8) == []


def test_detect_watering_near_end_has_no_bogus_plateau():
    """A watering detected at the window's end has no settled post-watering
    readings, so its plateau must be None (excluded from the wet-ceiling
    median) rather than the watering-day spike max."""
    means = [20, 19, 18, 17, 45]
    mins = [m - 1 for m in means]
    maxs = [m + 1 for m in means]
    maxs[4] = 48
    mrows = [row(t, mn, mx, mean)
             for t, mn, mx, mean in zip(dates(n=len(means)), mins, maxs, means)]
    events = cal.detect_waterings(mrows, jump_thresh=8)
    assert len(events) == 1
    assert events[0].plateau is None


# ---- compute_plant ---------------------------------------------------------

def test_compute_plant_uncalibrated_with_no_events():
    mrows = [row(t, 30, 31, 30) for t in dates(n=20)]
    res = compute(mrows, cur_mo=30)
    assert res.confidence == "uncalibrated"
    assert res.status == "UNCALIBRATED"
    assert res.dryness is None
    assert res.waterings_detected == 0


def test_compute_plant_slope_shown_when_uncalibrated_no_events():
    """The drydown rate is meaningful on its own, so it's published even for
    plants that have never seen a watering (uncalibrated). With a steady
    decline and no events, the slope is taken over the whole window."""
    means = [40, 38, 36, 34, 32, 30]
    mrows = [row(t, m - 1, m + 1, m)
             for t, m in zip(dates(n=len(means)), means)]
    res = compute(mrows, cur_mo=30)
    assert res.confidence == "uncalibrated"
    assert res.waterings_detected == 0
    assert res.slope_3d is not None and res.slope_3d == pytest.approx(-2.0)


def test_compute_plant_slope_shown_when_uncalibrated_events_none_valid():
    """Events detected but none valid (no dry-trigger) -> still uncalibrated,
    yet the current dry-down cycle's slope is published."""
    means = [42, 41, 40, 39, 47, 45, 44, 43, 42, 41]
    mins = [m - 1 for m in means]
    maxs = [m + 1 for m in means]
    maxs[4] = 50
    mrows = [row(t, mn, mx, mean)
             for t, mn, mx, mean in zip(dates(n=len(means)), mins, maxs, means)]
    res = compute(mrows, cur_mo=41)
    assert res.confidence == "uncalibrated"
    assert res.valid_waterings == 0
    assert res.slope_3d is not None and res.slope_3d < 0


def test_compute_plant_uncalibrated_when_events_but_none_valid():
    """Events but no *valid* (dry-trigger) watering -> uncalibrated, NOT 'low'."""
    means = [42, 41, 40, 39, 47, 45, 44, 43, 42, 41]
    mins = [m - 1 for m in means]
    maxs = [m + 1 for m in means]
    maxs[4] = 50
    mrows = [row(t, mn, mx, mean)
             for t, mn, mx, mean in zip(dates(n=len(means)), mins, maxs, means)]
    res = compute(mrows, cur_mo=41)
    assert res.waterings_detected >= 1
    assert res.valid_waterings == 0
    assert res.confidence == "uncalibrated"
    assert res.status == "UNCALIBRATED"
    assert res.dryness is None


def test_compute_plant_calibrated_high_confidence():
    seq = [45, 40, 35, 30, 25, 20, 15, 45, 40, 35,
           30, 25, 20, 15, 45, 40, 35, 30, 25, 20,
           15, 45, 40, 35, 30, 25]
    mins = [m - 2 for m in seq]
    maxs = [m + 2 for m in seq]
    for wi in (7, 14, 21):
        maxs[wi] = seq[wi] + 10
    mrows = [row(t, mn, mx, mean)
             for t, mn, mx, mean in zip(dates(n=len(seq)), mins, maxs, seq)]
    res = compute(mrows, cur_mo=20)
    assert res.confidence == "high"
    assert res.valid_waterings >= 3
    assert res.wet_ceiling is not None
    assert res.dry_floor is not None and res.dry_floor <= 20
    assert 0 < res.dryness < 100
    assert res.status in ("ok", "water soon", "WATER NOW")


def test_compute_plant_dryness_clamps_at_floor():
    seq = [45, 40, 35, 30, 25, 20, 15, 45, 40, 35, 30, 25, 20, 15, 45, 40]
    mins = [m - 2 for m in seq]
    maxs = [m + 2 for m in seq]
    for wi in (7, 14):
        maxs[wi] = seq[wi] + 10
    mrows = [row(t, mn, mx, mean)
             for t, mn, mx, mean in zip(dates(n=len(seq)), mins, maxs, seq)]
    res = compute(mrows, cur_mo=10)  # below floor
    assert res.dryness == 100
    assert res.status == "WATER NOW"


def test_compute_plant_just_watered_is_zero():
    seq = [45, 40, 35, 30, 25, 20, 15, 45, 40, 35, 30, 25, 20, 15, 45, 40]
    mins = [m - 2 for m in seq]
    maxs = [m + 2 for m in seq]
    for wi in (7, 14):
        maxs[wi] = seq[wi] + 10
    mrows = [row(t, mn, mx, mean)
             for t, mn, mx, mean in zip(dates(n=len(seq)), mins, maxs, seq)]
    res = compute(mrows, cur_mo=45)  # at ceiling
    assert res.dryness == 0
    assert res.status == "ok"


def test_compute_plant_slope_excludes_pre_watering_rows():
    """The 3-day slope must use only rows from the current dry-down cycle. With
    fewer than 3 post-watering reading-days, slope is None rather than reaching
    back across the watering boundary."""
    mrows = [
        row("2024-01-01", 43, 45, 44),
        row("2024-01-02", 39, 41, 40),
        row("2024-01-03", 35, 37, 36),
        row("2024-01-04", 15, 17, 16),
        row("2024-01-05", 15, 55, 45, last=45),  # watering day
        row("2024-01-06", 41, 43, 42),
        row("2024-01-10", 33, 35, 34),
    ]
    res = compute(mrows, cur_mo=34)
    assert res.slope_3d is None


def test_compute_plant_uses_latest_for_current_moisture():
    seq = [45, 40, 35, 30, 25, 20, 15, 45, 40, 35, 30, 25, 20, 15, 45, 40]
    mins = [m - 2 for m in seq]
    maxs = [m + 2 for m in seq]
    for wi in (7, 14):
        maxs[wi] = seq[wi] + 10
    mrows = [row(t, mn, mx, mean)
             for t, mn, mx, mean in zip(dates(n=len(seq)), mins, maxs, seq)]
    res = compute(mrows, cur_mo=20.0)
    assert res.moisture == 20.0


def test_compute_plant_jump_threshold_is_configurable():
    """A single jump threshold governs detection: a brief peak that clears the
    default threshold is caught, but a higher configured threshold ignores it.
    The spike is in the daily max only (mean stays on the gentle decline)."""
    means = [32, 31, 30, 29, 28, 27, 26]
    mins = [m - 1 for m in means]
    maxs = [m + 1 for m in means]
    maxs[4] = 40  # peak jumps +11 over prior peak (29): caught by 10, missed by 15
    mrows = [row(t, mn, mx, mean)
             for t, mn, mx, mean in zip(dates(n=len(means)), mins, maxs, means)]
    caught = compute([dict(r) for r in mrows], cur_mo=26, jump_threshold=10)
    missed = compute([dict(r) for r in mrows], cur_mo=26, jump_threshold=15)
    assert caught.waterings_detected == 1
    assert missed.waterings_detected == 0
