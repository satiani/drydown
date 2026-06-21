"""Tests for drydown's calibration math and watering detection.

These cover the pure-Python domain logic — the parts that actually compute
the product (water-need %, ETA, confidence tiers, watering detection). The
AppDaemon/InfluxDB I/O layer is stubbed; we feed daily aggregates directly
and assert on the computed result dicts.
"""

from __future__ import annotations

import datetime as dt
import types

import pytest

# conftest installs the appdaemon stub at import time.
import conftest  # noqa: F401

from apps.drydown import drydown as dd  # noqa: E402


# ---- helpers ---------------------------------------------------------------

def make_app(**overrides):
    """Build a Drydown instance without running AppDaemon's initialize()."""
    app = dd.Drydown.__new__(dd.Drydown)
    app.args = {}
    app._influx = {"host": "h", "port": 8086, "database": "db",
                   "username": "u", "password": "p"}
    app.sensors = {}
    app.lookback_days = 60
    app.jump_threshold = {"small": 15, "medium": 8, "large": 5}
    app.valid_floor_frac = 0.65
    app.conf_for_medium = 1
    app.conf_for_high = 3
    app.slope_reversal_margin = 1.0
    app.moisture_measurement = '"%"'
    app.conductivity_measurement = "/.*S.cm/"
    app.influx_retries = 3
    app.influx_backoff = 0  # no sleeping in tests
    app.dry_run = False
    app._current_state = {}        # entity_id -> state value
    app._current_attrs = {}        # entity_id -> attr dict
    app._set_states = {}

    # Wire get_state to the per-test override maps.
    def get_state(entity_id, attribute=None, default=None, **kwargs):
        if attribute is not None:
            return app._current_attrs.get(entity_id, {}).get(attribute, default)
        return app._current_state.get(entity_id, default)

    app.get_state = get_state
    for k, v in overrides.items():
        setattr(app, k, v)
    return app


def row(t, mn, mx, mean, cnt=24):
    """Build a daily-aggregate row like _pull_history produces."""
    return {"t": t, "mn": mn, "mx": mx, "mean": mean, "cnt": cnt}


def dates(start="2024-01-01", n=10):
    d = dt.date.fromisoformat(start)
    return [(d + dt.timedelta(days=i)).isoformat() for i in range(n)]


# ---- _linear_slope ---------------------------------------------------------


def test_bare_eid_strips_domain():
    assert dd.Drydown._bare_eid("sensor.plant_1_moisture") == "plant_1_moisture"
    assert dd.Drydown._bare_eid("plant_1_moisture") == "plant_1_moisture"
    # Only the first dot splits domain from object id.
    assert dd.Drydown._bare_eid("sensor.a.b_moisture") == "a.b_moisture"


def test_pull_history_keys_by_full_id_for_bare_tags():
    """HA's InfluxDB stores entity_id WITHOUT the domain prefix (domain is a
    separate tag). _pull_history must filter on the bare id and re-key the
    result by the full config id so _compute_plant's lookups work. Regression
    guard for the empty-history bug."""
    app = make_app()
    app.sensors = {
        "plant_1": {"moisture_entity": "sensor.plant_1_moisture"},
    }
    app.moisture_measurement = '"%"'
    app.conductivity_measurement = "/.*S.cm/"

    # Canned InfluxDB response keyed off the measurement in the query:
    # HA's schema stores the entity_id tag WITHOUT the domain prefix.
    def fake_query(q):
        if ".*S.cm" in q:
            tag = "plant_1_conductivity"
        else:
            tag = "plant_1_moisture"
        return {"series": [{
            "tags": {"entity_id": tag, "domain": "sensor"},
            "columns": ["time", "min", "max", "mean", "count"],
            "values": [
                ["2024-01-01T00:00:00Z", 10, 20, 15, 24],
                ["2024-01-02T00:00:00Z", 12, 22, 17, 24],
            ],
        }]}

    app._influx_query = fake_query
    history = app._pull_history()
    # Keyed by the full config id, not the bare tag.
    assert "sensor.plant_1_moisture" in history
    assert "plant_1_moisture" not in history
    assert len(history["sensor.plant_1_moisture"]) == 2
    assert history["sensor.plant_1_moisture"][0]["t"] == "2024-01-01"


def test_linear_slope_descending():
    # y = -2x + 10 -> slope -2
    ys = [10, 8, 6, 4, 2]
    assert dd.Drydown._linear_slope(ys) == pytest.approx(-2.0)


def test_linear_slope_flat():
    assert dd.Drydown._linear_slope([5, 5, 5]) == pytest.approx(0.0)


def test_linear_slope_degenerate():
    assert dd.Drydown._linear_slope([]) is None
    assert dd.Drydown._linear_slope([3]) is None
    # Zero variance in x is impossible with distinct integer indices >=2 pts,
    # but constant y still yields slope 0, not None.
    assert dd.Drydown._linear_slope([3, 3]) == pytest.approx(0.0)


# ---- _slope / _seg_slope ---------------------------------------------------

def test_slope_requires_three_points():
    app = make_app()
    rows = [row(t, 0, 0, 10) for t in dates(n=2)]
    assert app._slope(rows, n=7) is None  # only 2 points
    rows = [row(t, 0, 0, m) for t, m in zip(dates(n=3), [10, 8, 6])]
    assert app._slope(rows, n=7) == pytest.approx(-2.0)


def test_seg_slope_slice_bounds():
    app = make_app()
    rows = [row(t, 0, 0, m) for t, m in zip(dates(n=4), [10, 8, 6, 4])]
    # slice [0:2] -> indices 0,1 -> slope -2
    assert app._seg_slope(rows, 0, 2) == pytest.approx(-2.0)
    # negative start clamped to 0
    assert app._seg_slope(rows, -5, 2) == pytest.approx(-2.0)


# ---- _detect_waterings -----------------------------------------------------

def test_detect_watering_jump():
    app = make_app()
    # Flat dry-ish plateau, then a clear jump on day 4, then drain.
    means = [20, 19, 18, 17, 45, 43, 41, 39, 37]
    mins = [m - 1 for m in means]
    maxs = [m + 1 for m in means]
    maxs[4] = 48  # the watering day spikes in the max
    mrows = [row(t, mn, mx, mean)
             for t, mn, mx, mean in zip(dates(n=len(means)), mins, maxs, means)]
    events = app._detect_waterings(mrows, [], jump_thresh=8)
    assert len(events) == 1
    ev = events[0]
    assert ev["pre_min"] == mins[3]  # prev day's min
    assert ev["plateau"] >= 41       # settled post-watering mean is high


def test_detect_no_watering_when_steady():
    app = make_app()
    means = [30, 30, 30, 30, 30]
    mrows = [row(t, m - 1, m + 1, m) for t, m in zip(dates(n=5), means)]
    assert app._detect_waterings(mrows, [], jump_thresh=8) == []


def test_detect_watering_near_end_has_no_bogus_plateau():
    """A watering detected in the last days of the window has no settled
    post-watering readings, so its plateau must be None (excluded from the
    wet-ceiling median) rather than the watering-day spike max, which would
    inflate wet_ceiling. Regression guard."""
    app = make_app()
    # Watering spike on the very last day -> no future rows to settle on.
    means = [20, 19, 18, 17, 45]
    mins = [m - 1 for m in means]
    maxs = [m + 1 for m in means]
    maxs[4] = 48
    mrows = [row(t, mn, mx, mean)
             for t, mn, mx, mean in zip(dates(n=len(means)), mins, maxs, means)]
    events = app._detect_waterings(mrows, [], jump_thresh=8)
    assert len(events) == 1
    assert events[0]["plateau"] is None


# ---- _compute_plant --------------------------------------------------------

def _setup_plant(app, moist_entity, mrows, cur_mo, pot_size="medium"):
    app._current_state[moist_entity] = cur_mo
    app._current_attrs[moist_entity] = {"pot_size": pot_size}
    cond = moist_entity.replace("_moisture", "_conductivity")
    app._current_state[cond] = 100
    app._history = {moist_entity: mrows, cond: []}


def test_compute_plant_uncalibrated_with_no_events():
    app = make_app()
    mo = "sensor.p_moisture"
    # Steady, no watering jumps.
    mrows = [row(t, 30, 31, 30) for t in dates(n=20)]
    _setup_plant(app, mo, mrows, cur_mo=30)
    src = {"moisture_entity": mo}
    res = app._compute_plant("p", src, app._history)
    assert res["confidence"] == "uncalibrated"
    assert res["status"] == "UNCALIBRATED"
    assert res["need_pct"] is None
    assert res["waterings_detected"] == 0


def test_compute_plant_uncalibrated_when_events_but_none_valid():
    """Spec: events but no *valid* (dry-trigger) watering -> uncalibrated,
    NOT 'low'. Regression guard for the documented tier."""
    app = make_app()
    mo = "sensor.p_moisture"
    # Already-moist plant that gets watered again without drying first.
    # Wet ceiling ~45; pre-watering min ~40 is NOT < 0.65*45=29.25 -> invalid.
    means = [42, 41, 40, 39, 47, 45, 44, 43, 42, 41]
    mins = [m - 1 for m in means]
    maxs = [m + 1 for m in means]
    maxs[4] = 50
    mrows = [row(t, mn, mx, mean)
             for t, mn, mx, mean in zip(dates(n=len(means)), mins, maxs, means)]
    _setup_plant(app, mo, mrows, cur_mo=41)
    src = {"moisture_entity": mo}
    res = app._compute_plant("p", src, app._history)
    assert res["waterings_detected"] >= 1
    assert res["valid_waterings"] == 0
    assert res["confidence"] == "uncalibrated"   # NOT "low"
    assert res["status"] == "UNCALIBRATED"
    assert res["need_pct"] is None


def test_compute_plant_calibrated_high_confidence():
    app = make_app()
    mo = "sensor.p_moisture"
    # Three dry-then-water cycles. Floor ~15, ceiling ~45.
    # Pattern per cycle: drain down to ~15, jump to ~45, drain a bit.
    seq = (
        [45, 40, 35, 30, 25, 20, 15, 45, 40, 35,  # cycle 1 (water at idx 7)
         30, 25, 20, 15, 45, 40, 35, 30, 25, 20,  # cycle 2 (water at idx 14)
         15, 45, 40, 35, 30, 25]                   # cycle 3 (water at idx 21)
    )
    mins = [m - 2 for m in seq]
    maxs = [m + 2 for m in seq]
    # Make watering days' max clearly jump above prev mean + threshold.
    for wi in (7, 14, 21):
        maxs[wi] = seq[wi] + 10
    mrows = [row(t, mn, mx, mean)
             for t, mn, mx, mean in zip(dates(n=len(seq)), mins, maxs, seq)]
    _setup_plant(app, mo, mrows, cur_mo=20)
    src = {"moisture_entity": mo}
    res = app._compute_plant("p", src, app._history)

    assert res["confidence"] == "high"
    assert res["valid_waterings"] >= 3
    assert res["wet_ceiling"] is not None
    assert res["dry_floor"] is not None
    assert res["dry_floor"] <= 20  # floor is the driest pre-watering min
    # need = (wet - cur)/(wet - floor)*100, cur=20 between floor and wet.
    assert 0 < res["need_pct"] < 100
    assert res["status"] in ("ok", "water soon", "WATER NOW")


def test_compute_plant_water_need_clamps_at_floor():
    app = make_app()
    mo = "sensor.p_moisture"
    seq = [45, 40, 35, 30, 25, 20, 15, 45, 40, 35, 30, 25, 20, 15, 45, 40]
    mins = [m - 2 for m in seq]
    maxs = [m + 2 for m in seq]
    for wi in (7, 14):
        maxs[wi] = seq[wi] + 10
    mrows = [row(t, mn, mx, mean)
             for t, mn, mx, mean in zip(dates(n=len(seq)), mins, maxs, seq)]
    _setup_plant(app, mo, mrows, cur_mo=10)  # below floor -> clamp to 100
    src = {"moisture_entity": mo}
    res = app._compute_plant("p", src, app._history)
    assert res["need_pct"] == 100
    assert res["status"] == "WATER NOW"


def test_compute_plant_just_watered_is_zero():
    app = make_app()
    mo = "sensor.p_moisture"
    seq = [45, 40, 35, 30, 25, 20, 15, 45, 40, 35, 30, 25, 20, 15, 45, 40]
    mins = [m - 2 for m in seq]
    maxs = [m + 2 for m in seq]
    for wi in (7, 14):
        maxs[wi] = seq[wi] + 10
    mrows = [row(t, mn, mx, mean)
             for t, mn, mx, mean in zip(dates(n=len(seq)), mins, maxs, seq)]
    _setup_plant(app, mo, mrows, cur_mo=45)  # at ceiling -> 0
    src = {"moisture_entity": mo}
    res = app._compute_plant("p", src, app._history)
    assert res["need_pct"] == 0
    assert res["status"] == "ok"


def test_compute_plant_eta_from_slope():
    app = make_app()
    mo = "sensor.p_moisture"
    # Two dry-then-water cycles, then a clean 7-day linear drain of -2/day
    # at the tail so slope_7d is unambiguous.
    seq = (
        [45, 43, 41, 39, 37, 35, 33,               # drain (watering later)
         15, 45, 43, 41, 39, 37, 35, 33, 31, 29,   # water at idx 8, then drain
         15, 45, 43, 41, 39, 37, 35, 33, 31]       # water at idx 19, clean tail
    )
    mins = [m - 2 for m in seq]
    maxs = [m + 2 for m in seq]
    for wi in (8, 19):
        maxs[wi] = seq[wi] + 10
    mrows = [row(t, mn, mx, mean)
             for t, mn, mx, mean in zip(dates(n=len(seq)), mins, maxs, seq)]
    _setup_plant(app, mo, mrows, cur_mo=31)
    src = {"moisture_entity": mo}
    res = app._compute_plant("p", src, app._history)
    assert res["slope_7d"] is not None and res["slope_7d"] < 0
    assert res["slope_7d"] == pytest.approx(-2.0)
    assert res["eta_days"] is not None
    assert isinstance(res["eta_days"], float)
    assert res["eta_days"] >= 0


# ---- _write_entity ---------------------------------------------------------


def test_influx_query_retries_then_succeeds(monkeypatch):
    """Transient connection errors are retried; success returns the data."""
    app = make_app()
    calls = {"n": 0}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": [{"series": []}]}

    def fake_get(url, params=None, timeout=None):
        calls["n"] += 1
        if calls["n"] < 2:
            raise dd.requests.ConnectionError("boom")
        return FakeResp()

    monkeypatch.setattr(dd.requests, "get", fake_get)
    out = app._influx_query("SELECT 1")
    assert calls["n"] == 2
    assert out == {"series": []}


def test_influx_query_4xx_not_retried(monkeypatch):
    """A client error is not retried and returns {}."""
    app = make_app()
    calls = {"n": 0}

    class FakeResp:
        status_code = 400

    class FakeHTTPError(dd.requests.HTTPError):
        def __init__(self):
            super().__init__()
            self.response = FakeResp()

    def fake_get(url, params=None, timeout=None):
        calls["n"] += 1
        raise FakeHTTPError()

    monkeypatch.setattr(dd.requests, "get", fake_get)
    assert app._influx_query("SELECT 1") == {}
    assert calls["n"] == 1


def test_influx_query_5xx_retried(monkeypatch):
    """A 5xx is retried up to influx_retries, then returns {}."""
    app = make_app()
    app.influx_retries = 3

    class FakeResp:
        status_code = 503

    class FakeHTTPError(dd.requests.HTTPError):
        def __init__(self):
            super().__init__()
            self.response = FakeResp()

    def fake_get(url, params=None, timeout=None):
        raise FakeHTTPError()

    monkeypatch.setattr(dd.requests, "get", fake_get)
    assert app._influx_query("SELECT 1") == {}


# ---- _write_entity ---------------------------------------------------------

def test_write_entity_state_and_attributes():
    app = make_app()
    result = {
        "plant": "p1", "moisture_entity": "sensor.p1_moisture",
        "pot_size": "medium", "moisture": 20.0, "conductivity": 100.0,
        "wet_ceiling": 45.0, "dry_floor": 15.0, "need_pct": 67,
        "eta_days": 3.5, "confidence": "high", "status": "water soon",
        "slope_7d": -2.0, "waterings_detected": 4, "valid_waterings": 3,
    }
    app._write_entity("p1", result)
    written = app._set_states["sensor.drydown_p1"]
    assert written["state"] == 67
    assert written["attributes"]["confidence"] == "high"
    assert written["attributes"]["eta_days"] == 3.5
    assert written["attributes"]["friendly_name"] == "Drydown P1"


def test_write_entity_uncalibrated_state():
    app = make_app()
    result = {
        "plant": "p2", "moisture_entity": "sensor.p2_moisture",
        "pot_size": "medium", "moisture": None, "conductivity": None,
        "wet_ceiling": None, "dry_floor": None, "need_pct": None,
        "eta_days": "unknown", "confidence": "uncalibrated",
        "status": "UNCALIBRATED", "slope_7d": None,
        "waterings_detected": 0, "valid_waterings": 0,
    }
    app._write_entity("p2", result)
    assert app._set_states["sensor.drydown_p2"]["state"] == "uncalibrated"


def test_write_entity_dry_run_does_not_write():
    """In dry_run mode _write_entity logs and never calls set_state."""
    app = make_app()
    app.dry_run = True
    result = {
        "plant": "p3", "moisture_entity": "sensor.p3_moisture",
        "pot_size": "large", "moisture": 20.0, "conductivity": 100.0,
        "wet_ceiling": 45.0, "dry_floor": 15.0, "need_pct": 67,
        "eta_days": 3.5, "confidence": "high", "status": "water soon",
        "slope_7d": -2.0, "waterings_detected": 4, "valid_waterings": 3,
    }
    app._write_entity("p3", result)
    # No entity created.
    assert "sensor.drydown_p3" not in app._set_states


# ---- initialize scheduling -------------------------------------------------

def test_initialize_builds_hourly_start_time(monkeypatch):
    captured = {}

    class App(dd.Drydown):
        def run_in(self, cb, delay, **kw):
            pass

        def run_hourly(self, cb, start=None, **kw):
            captured["start"] = start

    app = App.__new__(App)
    app.args = {
        "influxdb": {"host": "h", "port": 8086, "database": "d",
                     "username": "u", "password": "p"},
        "sensors": {},
        "jump_threshold": {"small": 15, "medium": 8, "large": 5},
        "schedule": {"hourly_update": {"type": "hourly", "minute": 5}},
    }
    app.initialize()
    # The fix: minute is honored via a datetime.time, not a dropped kwarg.
    assert captured["start"] == dt.time(hour=0, minute=5)
