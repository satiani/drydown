"""Tests for the AppDaemon shell: config validation, scheduling, the run/pull
orchestration (per-plant error isolation, empty history), and publishing."""

from __future__ import annotations

import datetime as dt
import json

import conftest  # noqa: F401  (installs the appdaemon stub at import time)
import calibration as cal
import drydown as dd
import influx


# ---- helpers ---------------------------------------------------------------

def make_app(**overrides):
    """Build a Drydown instance without running AppDaemon's initialize()."""
    app = dd.Drydown.__new__(dd.Drydown)
    app.args = {}
    app.sensors = {}
    app.lookback_days = 60
    app.dry_run = False
    app.calibration_config = cal.CalibrationConfig()
    app.influx_config = influx.InfluxConfig(
        host="h", port=8086, database="db", username="u", password="p", backoff=0)
    app.influx = influx.InfluxClient(app.influx_config, app.log)
    app._calls = []

    def call_service(service, **kwargs):
        app._calls.append((service, kwargs))

    app.call_service = call_service
    for k, v in overrides.items():
        setattr(app, k, v)
    return app


def _result(**kw):
    base = dict(
        plant="p1", moisture_entity="sensor.p1_moisture",
        moisture=20.0, conductivity=100.0, wet_ceiling=45.0, dry_floor=15.0,
        dryness=67, eta_days=3.5, eta_reason=None, confidence="high",
        status="water soon", slope_3d=-2.0, waterings_detected=4,
        valid_waterings=3)
    base.update(kw)
    return cal.PlantResult(**base)


class RecordingApp(dd.Drydown):
    """Drydown subclass that records scheduler calls and log lines."""

    def __init__(self):
        self.run_in_calls = []
        self.run_hourly_calls = []
        self.logs = []

    def log(self, msg, *args, level="INFO", **kwargs):
        self.logs.append((level, msg % args if args else msg))

    def run_in(self, cb, delay, **kw):
        self.run_in_calls.append(delay)

    def run_hourly(self, cb, start=None, **kw):
        self.run_hourly_calls.append(start)


def base_args(**over):
    args = {
        "influxdb": {"host": "h", "port": 8086, "database": "d",
                     "username": "u", "password": "p"},
        "sensors": {},
        "schedule": {"hourly_update": {"type": "hourly", "minute": 5}},
        "dry_run": True,  # avoid the startup _run_all publishing path
    }
    args.update(over)
    return args


# ---- initialize: scheduling ------------------------------------------------

def test_initialize_builds_hourly_start_time():
    app = RecordingApp()
    app.args = base_args()
    app.initialize()
    assert app.run_hourly_calls == [dt.time(hour=0, minute=5)]
    assert app.run_in_calls == [30]


def test_initialize_unknown_schedule_type_warns_and_skips_hourly():
    app = RecordingApp()
    app.args = base_args(schedule={"hourly_update": {"type": "daily"}})
    app.initialize()
    assert app.run_hourly_calls == []          # no periodic schedule
    assert app.run_in_calls == [30]            # still runs once at startup
    assert any(lvl == "WARNING" and "unrecognized schedule" in m
               for lvl, m in app.logs)


# ---- initialize: config validation -----------------------------------------

def test_initialize_missing_sensors_key_aborts():
    app = RecordingApp()
    app.args = {"influxdb": {"host": "h", "port": 1, "database": "d",
                             "username": "u", "password": "p"}}
    app.initialize()
    assert app.run_in_calls == []  # never got to scheduling
    assert any(lvl == "ERROR" and "missing required config" in m
               for lvl, m in app.logs)


def test_initialize_missing_influx_field_aborts_with_message():
    app = RecordingApp()
    app.args = {"influxdb": {"host": "h"}, "sensors": {}}
    app.initialize()
    assert app.run_in_calls == []
    assert any(lvl == "ERROR" and "'influxdb' config missing" in m
               for lvl, m in app.logs)


def test_initialize_drops_sensor_without_moisture_entity():
    app = RecordingApp()
    app.args = base_args(sensors={
        "good": {"moisture_entity": "sensor.good_moisture"},
        "bad": {"some_other_key": "x"},
    })
    app.initialize()
    assert set(app.sensors) == {"good"}
    assert any(lvl == "ERROR" and "missing 'moisture_entity'" in m
               for lvl, m in app.logs)


# ---- _pull_history orchestration -------------------------------------------

def test_pull_history_merges_and_sorts_both_measurements():
    app = make_app(sensors={"p1": {"moisture_entity": "sensor.p1_moisture"}})

    def fake_query(q):
        tag = "p1_conductivity" if ".*S.cm" in q else "p1_moisture"
        return {"series": [{
            "tags": {"entity_id": tag},
            "columns": ["time", "min", "max", "mean", "last"],
            "values": [
                ["2024-01-02T00:00:00Z", 12, 22, 17, 22],
                ["2024-01-01T00:00:00Z", 10, 20, 15, 18],
            ],
        }]}

    app.influx.query = fake_query  # type: ignore[assignment]
    out = app._pull_history()
    assert set(out) == {"sensor.p1_moisture", "sensor.p1_conductivity"}
    # Rows sorted ascending by day even though InfluxDB returned them reversed.
    assert [r["t"] for r in out["sensor.p1_moisture"]] == \
        ["2024-01-01", "2024-01-02"]


# ---- _run_all error isolation + empty history ------------------------------

def test_run_all_skips_when_no_history():
    app = make_app(sensors={"p1": {"moisture_entity": "sensor.p1_moisture"}})
    app._pull_history = lambda: {}
    published = []
    app._publish_plant = lambda k, r: published.append(k)
    app._run_all({})
    assert published == []  # nothing published when history is empty


def test_run_all_isolates_per_plant_errors():
    """A plant whose computation raises must not stop the others."""
    app = make_app(sensors={
        "boom": {"moisture_entity": "sensor.boom_moisture"},
        "ok": {"moisture_entity": "sensor.ok_moisture"},
    })
    app._pull_history = lambda: {"sensor.boom_moisture": [],
                                 "sensor.ok_moisture": []}
    published = []
    app._publish_plant = lambda k, r: published.append(k)

    real_compute = cal.compute_plant

    def flaky_compute(plant_key, *a, **k):
        if plant_key == "boom":
            raise ValueError("kaboom")
        return real_compute(plant_key, *a, **k)

    cal.compute_plant = flaky_compute
    try:
        app._run_all({})
    finally:
        cal.compute_plant = real_compute
    assert published == ["ok"]  # the healthy plant still published


# ---- publishing ------------------------------------------------------------

def test_publish_plant_dry_run_does_not_publish():
    app = make_app(dry_run=True)
    app._publish_plant("p1", _result())
    assert app._calls == []


def test_publish_plant_publishes_all_payloads_via_service():
    app = make_app()
    app._publish_plant("p1", _result())
    assert [s for s, _ in app._calls] == ["mqtt/publish"] * 22
    topics = {kw["topic"] for _, kw in app._calls}
    assert "homeassistant/sensor/drydown/p1_dryness/config" in topics
    assert "drydown/p1/dryness/state" in topics
    assert all(kw["retain"] is True for _, kw in app._calls)
    state = next(kw["payload"] for _, kw in app._calls
                 if kw["topic"] == "drydown/p1/dryness/state")
    assert state == "67"
    config = next(kw["payload"] for _, kw in app._calls
                  if kw["topic"].endswith("p1_dryness/config"))
    assert json.loads(config)["name"] == "Dryness"


def test_conductivity_entity_derivation():
    assert dd.Drydown._conductivity_entity("sensor.p1_moisture") == \
        "sensor.p1_conductivity"
