"""drydown — normalized plant dryness indicator for Home Assistant.

Thin AppDaemon shell that wires config -> InfluxDB read -> pure calibration ->
MQTT publish. All domain logic lives in sibling modules:

  - ``influx``      — InfluxDB read layer (queries, retry, history pull)
  - ``calibration`` — pure dryness/watering math (no I/O)
  - ``publish``     — pure MQTT-discovery payload construction

The app is a pure InfluxDB -> MQTT transformer: it reads nothing from HA's
state machine and publishes entities via HA's ``mqtt.publish`` service (over
the websocket AppDaemon already maintains), so it holds no broker credentials.
HA's MQTT integration owns the entity/device registries; the app just asks HA
to publish retained discovery + state on its behalf. See README.md for the
full method.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from typing import Any

import appdaemon.plugins.hass.hassapi as hass
import calibration
import influx
import publish


class Drydown(hass.Hass):
    """AppDaemon app that publishes drydown plant devices over MQTT."""

    # ---- AppDaemon lifecycle ------------------------------------------------

    def initialize(self) -> None:
        self.log("drydown starting")
        cfg = self.args

        # ---- Validate config with actionable messages ----
        missing = [k for k in ("influxdb", "sensors") if k not in cfg]
        if missing:
            self.log("drydown: missing required config key(s): %s — app not "
                     "started", ", ".join(missing), level="ERROR")
            return
        influx_args = cfg["influxdb"]
        try:
            self.influx_config = influx.InfluxConfig(
                host=influx_args["host"],
                port=influx_args["port"],
                database=influx_args["database"],
                username=influx_args["username"],
                password=influx_args["password"],
                moisture_measurement=influx_args.get("moisture_measurement", '"%"'),
                conductivity_measurement=influx_args.get(
                    "conductivity_measurement", "/.*S.cm/"),
                retries=cfg.get("influx_retries", 3),
                backoff=cfg.get("influx_backoff", 1.0),
            )
        except KeyError as e:
            self.log("drydown: 'influxdb' config missing %s — app not started",
                     e, level="ERROR")
            return

        # Drop unusable sensors (missing moisture_entity) with a clear error,
        # but keep the good ones rather than aborting the whole app.
        self.sensors: dict[str, dict] = {}
        for key, src in cfg["sensors"].items():
            if "moisture_entity" not in src:
                self.log("drydown: sensor %s missing 'moisture_entity' — "
                         "skipping", key, level="ERROR")
                continue
            self.sensors[key] = src
        if not self.sensors:
            self.log("drydown: no usable sensors configured", level="WARNING")

        self.lookback_days = cfg.get("lookback_days", 60)
        # Read-only mode: compute + log results as JSON but never publish.
        self.dry_run = cfg.get("dry_run", False)

        self.influx = influx.InfluxClient(self.influx_config, self.log)
        self.calibration_config = calibration.CalibrationConfig(
            jump_threshold=cfg.get("jump_threshold", 10.0),
            valid_floor_frac=cfg.get("valid_watering_floor_fraction", 0.65),
            conf_for_high=cfg.get("confidence_valid_for_high", 3),
        )

        # Run once on startup so entities appear immediately.
        self.run_in(self._run_all, 30)
        self._schedule(cfg.get("schedule", {}))

        # Manual trigger: fire the `drydown_run` event in HA (e.g. from a
        # dashboard button via a script) to run immediately. This stays
        # event-driven, so the app still reads nothing from HA's state machine.
        self.listen_event(self._on_manual_trigger, "drydown_run")
        self.log("drydown: manual trigger listening for 'drydown_run' event")

        if self.dry_run:
            self.log("drydown DRY RUN enabled — nothing will be published")

    def _schedule(self, sched: dict) -> None:
        hu = sched.get("hourly_update", {"type": "hourly", "minute": 5})
        hu_type = hu.get("type")
        if hu_type != "hourly":
            self.log("drydown: unrecognized schedule type %r — running once at "
                     "startup only, no periodic updates", hu_type, level="WARNING")
            return
        # run_hourly's minute is taken from `start` (a datetime.time); there is
        # no `minute` kwarg. Build a time so :05 actually means :05.
        start = hu.get("start")
        if start is None:
            start = dt.time(hour=0, minute=int(hu.get("minute", 0)))
        self.run_hourly(self._run_all, start=start)
        self.log("drydown scheduled: hourly at :%02d", start.minute)

    # ---- Orchestration ------------------------------------------------------

    def _on_manual_trigger(self, event_name: str, data: dict[str, Any],
                           kwargs: Any) -> None:
        """Run immediately when the `drydown_run` HA event fires.

        Lets a dashboard button (wired to a script that fires this event)
        trigger a run on demand, without waiting for the hourly schedule.
        """
        self.log("drydown: manual trigger received via '%s' event", event_name)
        self._run_all(kwargs)

    def _run_all(self, kwargs: Any) -> None:
        """Pull history (+ latest) from InfluxDB, compute per-plant, publish."""
        try:
            self.log("drydown run starting")
            history = self._pull_history()
            if not history:
                self.log("drydown: no history pulled, skipping", level="WARNING")
                return
            for plant_key, src in self.sensors.items():
                moist = src["moisture_entity"]
                try:
                    result = calibration.compute_plant(
                        plant_key, moist,
                        history.get(moist, []),
                        history.get(self._conductivity_entity(moist), []),
                        self.calibration_config)
                except Exception as e:
                    self.log("drydown: error computing %s: %s", plant_key, e,
                             level="ERROR")
                    continue
                self._publish_plant(plant_key, result)
            self.log("drydown run complete")
        except Exception as e:
            self.log("drydown run failed: %s", e, level="ERROR")

    @staticmethod
    def _conductivity_entity(moisture_entity: str) -> str:
        """Derive a plant's conductivity entity id from its moisture id."""
        return moisture_entity.replace("_moisture", "_conductivity")

    def _pull_history(self) -> dict[str, list[dict]]:
        """Merge daily moisture + conductivity history keyed by full entity id."""
        now = dt.datetime.now(dt.timezone.utc)
        since = (now - dt.timedelta(days=self.lookback_days)).strftime("%Y-%m-%d")

        moisture_ids = [s["moisture_entity"] for s in self.sensors.values()]
        cond_ids = [self._conductivity_entity(m) for m in moisture_ids]

        out: dict[str, list[dict]] = {}
        for ids, measurement in (
                (moisture_ids, self.influx_config.moisture_measurement),
                (cond_ids, self.influx_config.conductivity_measurement)):
            part = influx.pull_daily_history(self.influx, ids, measurement, since)
            for eid, rows in part.items():
                out.setdefault(eid, []).extend(rows)

        for eid in out:
            out[eid].sort(key=lambda r: r["t"])
        return out

    # ---- MQTT publish -------------------------------------------------------

    def _publish_plant(self, plant_key: str,
                       result: calibration.PlantResult) -> None:
        if self.dry_run:
            self.log("DRYRUN_RESULT %s",
                     json.dumps(dataclasses.asdict(result), default=str,
                                sort_keys=True))
            return
        for topic, payload, retain in publish.build_payloads(plant_key, result):
            self._mqtt_publish(topic, payload, retain=retain)
        self.log("drydown %s: dryness=%s wet=%s dry=%s conf=%s",
                 plant_key, result.dryness, result.wet_ceiling,
                 result.dry_floor, result.confidence)

    def _mqtt_publish(self, topic: str, payload: str, retain: bool = False) -> None:
        """Publish via HA's mqtt.publish service over the websocket.

        HA's MQTT integration is already authenticated to the broker, so the
        app holds no broker credentials — it asks HA to publish on its behalf.
        Discovery (retained) and state both go through this path.
        """
        self.call_service("mqtt/publish", topic=topic, payload=payload,
                          retain=retain)
