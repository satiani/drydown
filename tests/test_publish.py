"""Tests for the pure MQTT-discovery payload construction."""

from __future__ import annotations

import json

import conftest  # noqa: F401  (installs the appdaemon stub at import time)
import calibration as cal
import publish


def _result(**kw):
    base = dict(
        plant="p1", moisture_entity="sensor.p1_moisture",
        moisture=20.0, conductivity=100.0, wet_ceiling=45.0, dry_floor=15.0,
        dryness=67, confidence="high",
        status="water soon", slope_3d=-2.0, waterings_detected=4,
        valid_waterings=3)
    base.update(kw)
    return cal.PlantResult(**base)


def test_payloads_structure():
    msgs = publish.build_payloads("plant_4", _result())
    # 10 metrics -> 20 messages (1 discovery + 1 state each), all retained.
    assert len(msgs) == 20
    assert all(r for _, _, r in msgs)
    disc = [t for t, _, _ in msgs if t.endswith("/config")]
    states = [t for t, _, _ in msgs if t.endswith("/state")]
    assert len(disc) == 10 and len(states) == 10


def test_payloads_device_block_ties_entities():
    msgs = publish.build_payloads("plant_4", _result())
    dev_jsons = {json.dumps(json.loads(p)["dev"])
                 for t, p, _ in msgs if t.endswith("/config")}
    assert len(dev_jsons) == 1
    dev = json.loads(next(iter(dev_jsons)))
    assert dev == {"identifiers": ["drydown_plant_4"],
                   "name": "Plant 4 Drydown", "manufacturer": "drydown",
                   "model": "Plant"}


def test_payloads_dryness_state_published():
    st = {t: p for t, p, _ in publish.build_payloads("p1", _result(dryness=67))
          if t.endswith("/state")}
    assert st["drydown/p1/dryness/state"] == "67"


def test_payloads_uncalibrated_publishes_empty_numeric_states():
    res = _result(dryness=None, wet_ceiling=None, dry_floor=None, moisture=None,
                  conductivity=None, slope_3d=None,
                  confidence="uncalibrated",
                  status="UNCALIBRATED", waterings_detected=0, valid_waterings=0)
    st = {t: p for t, p, _ in publish.build_payloads("p2", res)
          if t.endswith("/state")}
    assert st["drydown/p2/dryness/state"] == ""
    assert st["drydown/p2/wet_ceiling/state"] == ""
    assert st["drydown/p2/status/state"] == "UNCALIBRATED"
    assert st["drydown/p2/confidence/state"] == "uncalibrated"
    assert st["drydown/p2/waterings_detected/state"] == "0"


def test_payloads_entity_names_and_units():
    configs = {}
    for t, p, _ in publish.build_payloads("p1", _result()):
        if t.endswith("/config"):
            c = json.loads(p)
            configs[c["name"]] = c
    assert configs["Drydown rate (3d)"]["unit_of_meas"] == "%/d"
    assert "Dryness" in configs
    # old names must be gone
    for gone in ("ETA", "Slope 3d", "Slope 7d", "Water Need"):
        assert gone not in configs
