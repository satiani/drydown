"""Pure MQTT-discovery payload construction for drydown.

Builds the (topic, payload, retain) messages for one plant — one retained
discovery config + one retained state per metric — with no I/O, so it's
unit-testable without a broker. The AppDaemon app calls :func:`build_payloads`
and relays each message through HA's ``mqtt.publish`` service.

Every discovery config sets ``force_update: true``: HA otherwise suppresses
``state_changed`` for identical states, and the InfluxDB integration records
only on ``state_changed``, so a steady reading would write no points. With
``force_update`` each hourly publish yields one InfluxDB point per indicator.
"""

from __future__ import annotations

import json

from calibration import PlantResult


def metric_specs() -> list[dict]:
    """Per-entity MQTT discovery spec. `key` is the PlantResult attribute name.

    Entities with `stat_cla` are numeric (state must be a number or empty ->
    unavailable); entities without it are free-form strings (status,
    confidence).
    """
    return [
        {"obj": "dryness", "name": "Dryness", "key": "dryness",
         "dev_cla": "moisture", "unit": "%", "stat_cla": "measurement"},
        {"obj": "moisture", "name": "Moisture", "key": "moisture",
         "dev_cla": "moisture", "unit": "%", "stat_cla": "measurement"},
        {"obj": "conductivity", "name": "Conductivity", "key": "conductivity",
         "dev_cla": "conductivity", "unit": "µS/cm", "stat_cla": "measurement"},
        {"obj": "wet_ceiling", "name": "Wet Ceiling", "key": "wet_ceiling",
         "dev_cla": "moisture", "unit": "%", "icon": "mdi:water",
         "stat_cla": "measurement"},
        {"obj": "dry_floor", "name": "Dry Floor", "key": "dry_floor",
         "dev_cla": "moisture", "unit": "%", "icon": "mdi:water-outline",
         "stat_cla": "measurement"},
        {"obj": "drydown_rate_3d", "name": "Drydown rate (3d)", "key": "slope_3d",
         "unit": "%/d", "icon": "mdi:trending-down", "stat_cla": "measurement"},
        {"obj": "status", "name": "Status", "key": "status", "icon": "mdi:leaf"},
        {"obj": "confidence", "name": "Confidence", "key": "confidence",
         "icon": "mdi:shield-check"},
        {"obj": "waterings_detected", "name": "Waterings Detected",
         "key": "waterings_detected", "icon": "mdi:watering-can",
         "stat_cla": "measurement"},
        {"obj": "valid_waterings", "name": "Valid Waterings",
         "key": "valid_waterings", "icon": "mdi:water-check",
         "stat_cla": "measurement"},
    ]


def build_payloads(plant_key: str,
                   result: PlantResult) -> list[tuple[str, str, bool]]:
    """Build the (topic, payload, retain) messages for one plant.

    One retained discovery config + one retained state per metric. Discovery is
    published every run; it's idempotent (HA ignores re-publication) and
    survives broker restarts where retention was lost.
    """
    dev = {
        "identifiers": ["drydown_%s" % plant_key],
        # Suffix distinguishes this drydown device (derived/calibrated
        # entities) from the BLE integration's "Plant N Sensor" device (raw
        # readings). identifiers stay stable so HA renames in place instead of
        # creating a second device.
        "name": "%s Drydown" % plant_key.replace("_", " ").title(),
        "manufacturer": "drydown",
        "model": "Plant",
    }
    msgs: list[tuple[str, str, bool]] = []
    for spec in metric_specs():
        obj = spec["obj"]
        disc_topic = "homeassistant/sensor/drydown/%s_%s/config" % (plant_key, obj)
        state_topic = "drydown/%s/%s/state" % (plant_key, obj)
        config = {
            "name": spec["name"],
            "uniq_id": "drydown_%s_%s" % (plant_key, obj),
            "stat_t": state_topic,
            "dev": dev,
            # Force HA to fire a state_changed event on every received state
            # message even when the value is unchanged. HA's state machine
            # otherwise suppresses identical states, and since the InfluxDB
            # integration only records on state_changed, a steady reading (e.g.
            # dryness held at 81 for hours) would write no points. With
            # force_update, each hourly publish yields exactly one InfluxDB
            # point per indicator — giving continuous per-hour history.
            "force_update": True,
        }
        for ck, cv in (("dev_cla", "dev_cla"), ("unit", "unit_of_meas"),
                       ("stat_cla", "stat_cla"), ("icon", "icon")):
            if ck in spec:
                config[cv] = spec[ck]
        msgs.append((disc_topic, json.dumps(config), True))

        value = getattr(result, spec["key"])
        numeric = "stat_cla" in spec
        if numeric:
            # A unit-bearing entity must publish a number; None -> empty
            # (entity goes unavailable).
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            payload = str(value) if ok else ""
        else:
            payload = "" if value is None else str(value)
        msgs.append((state_topic, payload, True))
    return msgs
