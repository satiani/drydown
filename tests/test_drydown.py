"""Tests for drydown's calibration math, watering detection, and MQTT output.

These cover the pure-Python domain logic — the parts that compute the product
(dryness %, ETA, confidence tiers, watering detection) plus the MQTT payload
construction. The AppDaemon/InfluxDB/MQTT I/O layers are stubbed; we feed
daily aggregates + latest values directly and assert on result dicts and
the (topic, payload, retain) message lists.
"""

from __future__ import annotations

import datetime as dt
import json

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
    app._calls = []  # call_service captures here

    def call_service(service, **kwargs):
        app._calls.append((service, kwargs))

    app.call_service = call_service
    for k, v in overrides.items():
        setattr(app, k, v)
    return app


def row(t, mn, mx, mean, cnt=24, last=None):
    """Build a daily-aggregate row like _pull_history produces."""
    return {"t": t, "mn": mn, "mx": mx, "mean": mean, "last": mean if last is None else last, "cnt": cnt}


def dates(start="2024-01-01", n=10):
    d = dt.date.fromisoformat(start)
    return [(d + dt.timedelta(days=i)).isoformat() for i in range(n)]


def _setup_plant(app, moist_entity, mrows, cur_mo, pot_size="medium"):
    """Wire a plant's history into the app for _compute_plant.

    Returns (src, history) ready to pass to _compute_plant. The current
    reading is the `last` selector of the final moisture row, so cur_mo is
    stamped onto that row (matching how _pull_history + _last_value work)."""
    cond = moist_entity.replace("_moisture", "_conductivity")
    src = {"moisture_entity": moist_entity, "pot_size": pot_size}
    if mrows:
        mrows[-1] = dict(mrows[-1], last=cur_mo)
    history = {moist_entity: mrows, cond: []}
    return src, history


# ---- _bare_eid / _entity_filter -------------------------------------------

def test_bare_eid_strips_domain():
    assert dd.Drydown._bare_eid("sensor.plant_1_moisture") == "plant_1_moisture"
    assert dd.Drydown._bare_eid("plant_1_moisture") == "plant_1_moisture"
    # Only the first dot splits domain from object id.
    assert dd.Drydown._bare_eid("sensor.a.b_moisture") == "a.b_moisture"


def test_entity_filter_matches_bare_or_full():
    app = make_app()
    ids = ["sensor.plant_1_moisture", "plant_2_moisture"]
    f = app._entity_filter(ids)
    # Bare id form gets an OR; already-bare id gets a single equality.
    assert '"entity_id" = \'plant_1_moisture\'' in f
    assert '"entity_id" = \'sensor.plant_1_moisture\'' in f
    assert '"entity_id" = \'plant_2_moisture\'' in f
    assert " OR " in f


# ---- _pull_history / _last_value (bare-tag keying) -----------------------

def test_pull_history_keys_by_full_id_for_bare_tags():
    """HA's InfluxDB stores entity_id WITHOUT the domain prefix (domain is a
    separate tag). _pull_history must filter on the bare id and re-key the
    result by the full config id so _compute_plant's lookups work. Regression
    guard for the empty-history bug."""
    app = make_app()
    app.sensors = {
        "plant_1": {"moisture_entity": "sensor.plant_1_moisture"},
    }

    # Canned InfluxDB response keyed off the measurement in the query:
    # HA's schema stores the entity_id tag WITHOUT the domain prefix.
    # Columns match the merged SELECT: min, max, mean, last, count.
    def fake_query(q):
        if ".*S.cm" in q:
            tag = "plant_1_conductivity"
        else:
            tag = "plant_1_moisture"
        return {"series": [{
            "tags": {"entity_id": tag, "domain": "sensor"},
            "columns": ["time", "min", "max", "mean", "last", "count"],
            "values": [
                ["2024-01-01T00:00:00Z", 10, 20, 15, 18, 24],
                ["2024-01-02T00:00:00Z", 12, 22, 17, 22, 24],
            ],
        }]}

    app._influx_query = fake_query
    history = app._pull_history()
    assert "sensor.plant_1_moisture" in history
    assert "plant_1_moisture" not in history
    assert len(history["sensor.plant_1_moisture"]) == 2
    assert history["sensor.plant_1_moisture"][0]["t"] == "2024-01-01"
    # The merged query carries the per-day `last` selector used as "current".
    assert history["sensor.plant_1_moisture"][1]["last"] == 22


def test_last_value_takes_final_non_null_last():
    """_last_value returns the most recent non-null `last` across daily rows
    — the entity's current reading, replacing the old separate last() query."""
    app = make_app()
    rows = [
        {"t": "2024-01-01", "last": 18},
        {"t": "2024-01-02", "last": 22},
    ]
    assert app._last_value(rows) == 22


def test_last_value_skips_trailing_null_last():
    """A trailing day with null `last` (no readings) is skipped in favor of
    the previous day's last; an all-null/empty history returns None."""
    app = make_app()
    assert app._last_value([
        {"t": "d1", "last": 30},
        {"t": "d2", "last": None},
    ]) == 30
    assert app._last_value([{"t": "d1", "last": None}]) is None
    assert app._last_value([]) is None


# ---- _linear_slope ---------------------------------------------------------

def test_linear_slope_descending():
    # y = -2x + 10 -> slope -2
    ys = [10, 8, 6, 4, 2]
    assert dd.Drydown._linear_slope(ys) == pytest.approx(-2.0)


def test_linear_slope_flat():
    assert dd.Drydown._linear_slope([5, 5, 5]) == pytest.approx(0.0)


def test_linear_slope_degenerate():
    assert dd.Drydown._linear_slope([]) is None
    assert dd.Drydown._linear_slope([3]) is None
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
    assert app._seg_slope(rows, 0, 2) == pytest.approx(-2.0)
    assert app._seg_slope(rows, -5, 2) == pytest.approx(-2.0)


# ---- _detect_waterings -----------------------------------------------------

def test_detect_watering_jump():
    app = make_app()
    means = [20, 19, 18, 17, 45, 43, 41, 39, 37]
    mins = [m - 1 for m in means]
    maxs = [m + 1 for m in means]
    maxs[4] = 48  # the watering day spikes in the max
    mrows = [row(t, mn, mx, mean)
             for t, mn, mx, mean in zip(dates(n=len(means)), mins, maxs, means)]
    events = app._detect_waterings(mrows, [], jump_thresh=8)
    assert len(events) == 1
    ev = events[0]
    assert ev["pre_min"] == mins[3]
    assert ev["plateau"] >= 41


def test_detect_no_watering_when_steady():
    app = make_app()
    means = [30, 30, 30, 30, 30]
    mrows = [row(t, m - 1, m + 1, m) for t, m in zip(dates(n=5), means)]
    assert app._detect_waterings(mrows, [], jump_thresh=8) == []


def test_detect_watering_near_end_has_no_bogus_plateau():
    """A watering detected in the last days of the window has no settled
    post-watering readings, so its plateau must be None (excluded from the
    wet-ceiling median) rather than the watering-day spike max."""
    app = make_app()
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

def test_compute_plant_uncalibrated_with_no_events():
    app = make_app()
    mo = "sensor.p_moisture"
    mrows = [row(t, 30, 31, 30) for t in dates(n=20)]
    src, history = _setup_plant(app, mo, mrows, cur_mo=30)
    res = app._compute_plant("p", src, history)
    assert res["confidence"] == "uncalibrated"
    assert res["status"] == "UNCALIBRATED"
    assert res["dryness"] is None
    assert res["waterings_detected"] == 0


def test_compute_plant_uncalibrated_when_events_but_none_valid():
    """Spec: events but no *valid* (dry-trigger) watering -> uncalibrated,
    NOT 'low'. Regression guard for the documented tier."""
    app = make_app()
    mo = "sensor.p_moisture"
    means = [42, 41, 40, 39, 47, 45, 44, 43, 42, 41]
    mins = [m - 1 for m in means]
    maxs = [m + 1 for m in means]
    maxs[4] = 50
    mrows = [row(t, mn, mx, mean)
             for t, mn, mx, mean in zip(dates(n=len(means)), mins, maxs, means)]
    src, history = _setup_plant(app, mo, mrows, cur_mo=41)
    res = app._compute_plant("p", src, history)
    assert res["waterings_detected"] >= 1
    assert res["valid_waterings"] == 0
    assert res["confidence"] == "uncalibrated"   # NOT "low"
    assert res["status"] == "UNCALIBRATED"
    assert res["dryness"] is None


def test_compute_plant_calibrated_high_confidence():
    app = make_app()
    mo = "sensor.p_moisture"
    seq = (
        [45, 40, 35, 30, 25, 20, 15, 45, 40, 35,
         30, 25, 20, 15, 45, 40, 35, 30, 25, 20,
         15, 45, 40, 35, 30, 25]
    )
    mins = [m - 2 for m in seq]
    maxs = [m + 2 for m in seq]
    for wi in (7, 14, 21):
        maxs[wi] = seq[wi] + 10
    mrows = [row(t, mn, mx, mean)
             for t, mn, mx, mean in zip(dates(n=len(seq)), mins, maxs, seq)]
    src, history = _setup_plant(app, mo, mrows, cur_mo=20)
    res = app._compute_plant("p", src, history)
    assert res["confidence"] == "high"
    assert res["valid_waterings"] >= 3
    assert res["wet_ceiling"] is not None
    assert res["dry_floor"] is not None
    assert res["dry_floor"] <= 20
    assert 0 < res["dryness"] < 100
    assert res["status"] in ("ok", "water soon", "WATER NOW")


def test_compute_plant_dryness_clamps_at_floor():
    app = make_app()
    mo = "sensor.p_moisture"
    seq = [45, 40, 35, 30, 25, 20, 15, 45, 40, 35, 30, 25, 20, 15, 45, 40]
    mins = [m - 2 for m in seq]
    maxs = [m + 2 for m in seq]
    for wi in (7, 14):
        maxs[wi] = seq[wi] + 10
    mrows = [row(t, mn, mx, mean)
             for t, mn, mx, mean in zip(dates(n=len(seq)), mins, maxs, seq)]
    src, history = _setup_plant(app, mo, mrows, cur_mo=10)  # below floor
    res = app._compute_plant("p", src, history)
    assert res["dryness"] == 100
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
    src, history = _setup_plant(app, mo, mrows, cur_mo=45)  # at ceiling
    res = app._compute_plant("p", src, history)
    assert res["dryness"] == 0
    assert res["status"] == "ok"


def test_compute_plant_eta_from_slope():
    app = make_app()
    mo = "sensor.p_moisture"
    seq = (
        [45, 43, 41, 39, 37, 35, 33,
         15, 45, 43, 41, 39, 37, 35, 33, 31, 29,
         15, 45, 43, 41, 39, 37, 35, 33, 31]
    )
    mins = [m - 2 for m in seq]
    maxs = [m + 2 for m in seq]
    for wi in (8, 19):
        maxs[wi] = seq[wi] + 10
    mrows = [row(t, mn, mx, mean)
             for t, mn, mx, mean in zip(dates(n=len(seq)), mins, maxs, seq)]
    src, history = _setup_plant(app, mo, mrows, cur_mo=31)
    res = app._compute_plant("p", src, history)
    assert res["slope_3d"] is not None and res["slope_3d"] < 0
    assert res["slope_3d"] == pytest.approx(-2.0)
    assert res["eta_days"] is not None
    assert isinstance(res["eta_days"], float)
    assert res["eta_days"] >= 0


def test_compute_plant_uses_latest_for_current_moisture():
    """_compute_plant reads current moisture from the history's final `last`
    selector (InfluxDB last()), not from HA state. Regression guard for the
    InfluxDB-only path."""
    app = make_app()
    mo = "sensor.p_moisture"
    seq = [45, 40, 35, 30, 25, 20, 15, 45, 40, 35, 30, 25, 20, 15, 45, 40]
    mins = [m - 2 for m in seq]
    maxs = [m + 2 for m in seq]
    for wi in (7, 14):
        maxs[wi] = seq[wi] + 10
    mrows = [row(t, mn, mx, mean)
             for t, mn, mx, mean in zip(dates(n=len(seq)), mins, maxs, seq)]
    src, history = _setup_plant(app, mo, mrows, cur_mo=20.0)
    # current value comes from the final moisture row's `last`, not any HA state
    res = app._compute_plant("p", src, history)
    assert res["moisture"] == 20.0


# ---- InfluxDB retry --------------------------------------------------------

def test_influx_query_retries_then_succeeds(monkeypatch):
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


# ---- _plant_mqtt_payloads --------------------------------------------------

def _result(**kw):
    base = {
        "plant": "p1", "moisture_entity": "sensor.p1_moisture",
        "pot_size": "medium", "moisture": 20.0, "conductivity": 100.0,
        "wet_ceiling": 45.0, "dry_floor": 15.0, "dryness": 67,
        "eta_days": 3.5, "confidence": "high", "status": "water soon",
        "slope_3d": -2.0, "waterings_detected": 4, "valid_waterings": 3,
    }
    base.update(kw)
    return base


def test_mqtt_payloads_structure():
    app = make_app()
    msgs = app._plant_mqtt_payloads("plant_4", _result())
    # 11 metrics -> 22 messages (1 discovery + 1 state each), all retained.
    assert len(msgs) == 22
    assert all(r for _, _, r in msgs)
    disc = [(t, p) for t, p, _ in msgs if t.endswith("/config")]
    states = [(t, p) for t, p, _ in msgs if t.endswith("/state")]
    assert len(disc) == 11 and len(states) == 11


def test_mqtt_payloads_device_block_ties_entities():
    app = make_app()
    msgs = app._plant_mqtt_payloads("plant_4", _result())
    # Every discovery message carries the same device block.
    dev_jsons = set()
    for topic, payload, _ in msgs:
        if topic.endswith("/config"):
            dev_jsons.add(json.dumps(json.loads(payload)["dev"]))
    assert len(dev_jsons) == 1
    dev = json.loads(next(iter(dev_jsons)))
    assert dev == {"identifiers": ["drydown_plant_4"], "name": "Plant 4",
                   "manufacturer": "drydown", "model": "Plant"}


def test_mqtt_payloads_dryness_state_published():
    app = make_app()
    msgs = app._plant_mqtt_payloads("p1", _result(dryness=67))
    st = {t: p for t, p, _ in msgs if t.endswith("/state")}
    assert st["drydown/p1/dryness/state"] == "67"


def test_mqtt_payloads_eta_numeric_only():
    """ETA entity has unit 'd'; a numeric ETA publishes the number, but
    'rising'/'unknown'/None publishes empty (entity goes unavailable)."""
    app = make_app()
    # numeric
    msgs = app._plant_mqtt_payloads("p1", _result(eta_days=3.5))
    st = {t: p for t, p, _ in msgs if t.endswith("/state")}
    assert st["drydown/p1/next_watering_estimate/state"] == "3.5"
    # string -> empty
    msgs = app._plant_mqtt_payloads("p1", _result(eta_days="rising"))
    st = {t: p for t, p, _ in msgs if t.endswith("/state")}
    assert st["drydown/p1/next_watering_estimate/state"] == ""
    # None -> empty
    msgs = app._plant_mqtt_payloads("p1", _result(eta_days=None))
    st = {t: p for t, p, _ in msgs if t.endswith("/state")}
    assert st["drydown/p1/next_watering_estimate/state"] == ""


def test_mqtt_payloads_uncalibrated_publishes_empty_numeric_states():
    """Uncalibrated plant: dryness/wet_ceiling/etc are None -> empty state
    (unavailable); status/confidence still publish their strings."""
    app = make_app()
    res = _result(dryness=None, wet_ceiling=None, dry_floor=None,
                  moisture=None, conductivity=None, slope_3d=None,
                  eta_days="unknown", confidence="uncalibrated",
                  status="UNCALIBRATED", waterings_detected=0, valid_waterings=0)
    st = {t: p for t, p, _ in app._plant_mqtt_payloads("p2", res)
          if t.endswith("/state")}
    assert st["drydown/p2/dryness/state"] == ""
    assert st["drydown/p2/wet_ceiling/state"] == ""
    assert st["drydown/p2/status/state"] == "UNCALIBRATED"
    assert st["drydown/p2/confidence/state"] == "uncalibrated"
    assert st["drydown/p2/waterings_detected/state"] == "0"


def test_mqtt_payloads_renamed_entities_present():
    """ETA -> 'Next Watering Estimate'; Slope 3d -> 'Drydown rate (3d)'."""
    app = make_app()
    configs = {}
    for topic, payload, _ in app._plant_mqtt_payloads("p1", _result()):
        if topic.endswith("/config"):
            c = json.loads(payload)
            configs[c["name"]] = c
    assert "Next Watering Estimate" in configs
    assert configs["Next Watering Estimate"]["dev_cla"] == "duration"
    assert configs["Next Watering Estimate"]["unit_of_meas"] == "d"
    assert "Drydown rate (3d)" in configs
    assert configs["Drydown rate (3d)"]["unit_of_meas"] == "%/d"
    # old names gone
    assert "ETA" not in configs
    assert "Slope 3d" not in configs
    assert "Slope 7d" not in configs
    assert "Water Need" not in configs
    assert "Dryness" in configs


def test_publish_plant_dry_run_does_not_publish():
    """In dry_run mode _publish_plant logs and never calls mqtt/publish."""
    app = make_app()
    app.dry_run = True
    app._publish_plant("p1", _result())
    assert app._calls == []  # nothing published


def test_publish_plant_publishes_all_payloads_via_service():
    """Publishing goes through HA's mqtt/publish service, one call per message."""
    app = make_app()
    app._publish_plant("p1", _result())
    services = [s for s, _ in app._calls]
    assert all(s == "mqtt/publish" for s in services)
    assert len(app._calls) == 22  # 11 metrics x (1 discovery + 1 state)
    # Spot-check: discovery config is retained + JSON; state is retained.
    dryness_calls = [(kw["topic"], kw["payload"], kw["retain"])
                     for _, kw in app._calls
                     if kw["topic"].endswith("_dryness/config")
                     or kw["topic"] == "drydown/p1/dryness/state"]
    topics = {t for t, _, _ in dryness_calls}
    assert "homeassistant/sensor/drydown/p1_dryness/config" in topics
    assert "drydown/p1/dryness/state" in topics
    assert all(r is True for _, _, r in dryness_calls)
    # State payload is the numeric string; config payload parses as JSON.
    state_payload = next(p for t, p, _ in dryness_calls
                         if t == "drydown/p1/dryness/state")
    assert state_payload == "67"
    config_payload = next(p for t, p, _ in dryness_calls
                          if t.endswith("_dryness/config"))
    assert json.loads(config_payload)["name"] == "Dryness"


# ---- initialize scheduling -------------------------------------------------

def test_initialize_builds_hourly_start_time():
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
        "dry_run": True,  # avoid the startup _run_all publishing path
    }
    app.initialize()
    assert captured["start"] == dt.time(hour=0, minute=5)
