"""drydown — normalized plant dryness indicator for Home Assistant.

Reads moisture/conductivity history + latest readings from InfluxDB, detects
watering events from moisture jumps, learns a per-plant wet ceiling and dry
floor, computes a 0–100 dryness %, and publishes one Home Assistant **device**
per plant (via MQTT discovery) with one sensor entity per metric. Each entity
gets its own history. See README.md for the full method.

The app is a pure InfluxDB → MQTT transformer: it reads nothing from HA's
state machine and publishes entities via HA's `mqtt.publish` service (over the
websocket AppDaemon already maintains), so it holds no broker credentials.
HA's MQTT integration owns the entity/device registries; the app just asks HA
to publish retained discovery + state on its behalf.
"""

import datetime as dt
import json
import statistics
import time

import appdaemon.plugins.hass.hassapi as hass
import requests


class Drydown(hass.Hass):
    """AppDaemon app that publishes drydown plant devices over MQTT."""

    # ---- AppDaemon lifecycle ------------------------------------------------

    def initialize(self):
        self.log("drydown starting")
        self._influx = self.args["influxdb"]
        self.sensors = self.args["sensors"]
        self.lookback_days = self.args.get("lookback_days", 60)
        self.jump_threshold = self.args.get(
            "jump_threshold", {"small": 15, "medium": 8, "large": 5})
        self.valid_floor_frac = self.args.get("valid_watering_floor_fraction", 0.65)
        self.conf_for_medium = self.args.get("confidence_valid_for_medium", 1)
        self.conf_for_high = self.args.get("confidence_valid_for_high", 3)
        self.slope_reversal_margin = self.args.get("slope_reversal_margin", 1.0)
        # InfluxDB measurement names to query, as raw FROM-clause tokens:
        # a quoted literal (e.g. '"%"') or a /regex/. Defaults match the HA
        # InfluxDB integration, which names measurements by unit_of_measurement.
        self.moisture_measurement = self._influx.get("moisture_measurement", '"%"')
        self.conductivity_measurement = self._influx.get(
            "conductivity_measurement", "/.*S.cm/")
        self.influx_retries = self.args.get("influx_retries", 3)
        self.influx_backoff = self.args.get("influx_backoff", 1.0)
        # Read-only mode: compute everything and log results as JSON, but never
        # publish (so no entities are created or modified). Used to validate
        # calibration against live data without side effects.
        self.dry_run = self.args.get("dry_run", False)

        sched = self.args.get("schedule", {})
        hu = sched.get("hourly_update", {"type": "hourly", "minute": 5})

        # Run once on startup so entities appear immediately.
        self.run_in(self._run_all, 30)

        if hu.get("type") == "hourly":
            # run_hourly's minute is taken from `start` (a datetime.time);
            # there is no `minute` kwarg. Build a time so :05 actually means :05.
            start = hu.get("start")
            if start is None:
                start = dt.time(hour=0, minute=int(hu.get("minute", 0)))
            self.run_hourly(self._run_all, start=start)

        self.log("drydown scheduled: hourly at :%02d", int(hu.get("minute", 0)))
        if self.dry_run:
            self.log("drydown DRY RUN enabled — nothing will be published")

    # ---- Orchestration ------------------------------------------------------

    def _run_all(self, kwargs):
        """Pull history (+ latest) from InfluxDB, compute per-plant, publish."""
        try:
            self.log("drydown run starting")
            history = self._pull_history()
            if not history:
                self.log("drydown: no history pulled, skipping", level="WARNING")
                return
            for plant_key, src in self.sensors.items():
                try:
                    result = self._compute_plant(plant_key, src, history)
                except Exception as e:
                    self.log("drydown: error computing %s: %s", plant_key, e, level="ERROR")
                    continue
                self._publish_plant(plant_key, result)
            self.log("drydown run complete")
        except Exception as e:
            self.log("drydown run failed: %s", e, level="ERROR")

    # ---- InfluxDB pull ------------------------------------------------------

    def _entity_filter(self, ids):
        """Build an OR clause of entity_id filters matching bare OR full id.

        HA's InfluxDB integration stores `domain` and `entity_id` as separate
        tags, so the entity_id tag is the bare id (e.g. 'plant_1_moisture',
        not 'sensor.plant_1_moisture'). Match either form to be robust.
        """
        clauses = []
        for e in ids:
            b = self._bare_eid(e)
            if b == e:
                clauses.append(f'"entity_id" = \'{e}\'')
            else:
                clauses.append(f'("entity_id" = \'{b}\' OR "entity_id" = \'{e}\')')
        return " OR ".join(clauses)

    def _pull_history(self):
        """Return {entity_id: [{'t','mn','mx','mean','last','cnt'}, ...]}.

        Daily aggregates plus the latest point per day for moisture and
        conductivity over the lookback window, in two batched queries. The
        per-day `last` selector also serves as the "current" reading: the
        single most recent point overall is the `last` of the final non-empty
        day's bucket (see _last_value), so a separate last() query is
        unnecessary — one query per measurement does both jobs.

        All time math is UTC: the cutoff is UTC midnight and InfluxDB's
        time(1d) buckets are UTC by default, so day boundaries are
        deterministic regardless of AppDaemon's tz.
        """
        now = dt.datetime.now(dt.timezone.utc)
        since = (now - dt.timedelta(days=self.lookback_days)).strftime("%Y-%m-%d")
        out = {}

        moisture_ids = [s["moisture_entity"] for s in self.sensors.values()]
        cond_ids = [m.replace("_moisture", "_conductivity") for m in moisture_ids]

        for ids, measurement in [(moisture_ids, self.moisture_measurement),
                                 (cond_ids, self.conductivity_measurement)]:
            if not ids:
                continue
            ors = self._entity_filter(ids)
            q = (f'SELECT min("value"), max("value"), mean("value"), '
                 f'last("value"), count("value") '
                 f'FROM {measurement} WHERE ({ors}) AND time >= \'{since}\' '
                 f'GROUP BY time(1d), "entity_id" fill(null)')
            data = self._influx_query(q)
            bare_to_full = {self._bare_eid(e): e for e in ids}
            for series in data.get("series", []):
                # Re-key by the full config entity_id so the rest of the app
                # (which holds full ids) can look history up directly.
                eid_tag = series.get("tags", {}).get("entity_id")
                eid = bare_to_full.get(eid_tag, eid_tag)
                cols = series["columns"]
                rows = []
                for v in series["values"]:
                    rec = dict(zip(cols, v))
                    if rec.get("min") is None:
                        continue
                    rows.append({
                        # time(1d) buckets -> one row per UTC day, so [:10]
                        # strips the identical T00:00:00Z suffix.
                        "t": rec["time"][:10],
                        "mn": rec["min"],
                        "mx": rec["max"],
                        "mean": rec["mean"],
                        "last": rec["last"],
                        "cnt": rec["count"],
                    })
                out.setdefault(eid, []).extend(rows)

        for eid in out:
            out[eid].sort(key=lambda r: r["t"])
        return out

    @staticmethod
    def _last_value(rows):
        """Return the most recent non-null `last` selector across daily rows.

        The per-day `last` carried by _pull_history is the latest point in
        that day's bucket, so the most recent non-null one across the window
        is the entity's current reading — no separate last() query needed.
        """
        for r in reversed(rows):
            if r.get("last") is not None:
                return r["last"]
        return None

    def _influx_query(self, q):
        url = "http://%s:%s/query" % (self._influx["host"], self._influx["port"])
        params = {
            "db": self._influx["database"],
            "u": self._influx["username"],
            "p": self._influx["password"],
            "q": q,
        }
        r = self._influx_get_with_retry(url, params)
        if r is None:
            return {}
        data = r.json()
        results = data.get("results", [{}])
        result = results[0] if results else {}
        if "error" in result:
            self.log("drydown: InfluxDB query error: %s", result["error"], level="ERROR")
            return {}
        return result

    def _influx_get_with_retry(self, url, params):
        """GET with bounded exponential backoff on transient failures.

        Retries connection errors, timeouts, and 5xx. A 4xx (bad query) is
        not retried — it won't fix itself. Returns the Response or None.
        """
        last_exc = None
        for attempt in range(self.influx_retries):
            try:
                r = requests.get(url, params=params, timeout=30)
                r.raise_for_status()
                return r
            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
            except requests.HTTPError as e:
                if e.response is not None and 500 <= e.response.status_code < 600:
                    last_exc = e
                else:
                    self.log("drydown: InfluxDB client error %s: %s",
                             e.response.status_code if e.response else "?",
                             e, level="ERROR")
                    return None
            if attempt < self.influx_retries - 1:
                wait = self.influx_backoff * (2 ** attempt)
                self.log("drydown: InfluxDB request failed (attempt %d/%d): %s; "
                         "retrying in %.1fs", attempt + 1, self.influx_retries,
                         last_exc, wait, level="WARNING")
                time.sleep(wait)
        self.log("drydown: InfluxDB request gave up after %d attempts: %s",
                 self.influx_retries, last_exc, level="ERROR")
        return None

    @staticmethod
    def _bare_eid(entity_id):
        """Return entity_id without its HA domain prefix.

        HA's InfluxDB integration stores `domain` and `entity_id` as separate
        tags, so the entity_id tag is the bare id (e.g. 'plant_1_moisture',
        not 'sensor.plant_1_moisture'). We match on the bare id when querying.
        """
        return entity_id.split(".", 1)[1] if "." in entity_id else entity_id

    # ---- Per-plant computation ---------------------------------------------

    def _compute_plant(self, plant_key, src, history):
        moist_entity = src["moisture_entity"]
        cond_entity = moist_entity.replace("_moisture", "_conductivity")

        mrows = history.get(moist_entity, [])
        crows = history.get(cond_entity, [])
        cur_mo = self._last_value(mrows)
        cur_co = self._last_value(crows)

        pot_size = src.get("pot_size", "medium")
        jump_thresh = self.jump_threshold.get(pot_size, self.jump_threshold["medium"])

        result = {
            "plant": plant_key,
            "moisture_entity": moist_entity,
            "pot_size": pot_size,
            "moisture": cur_mo,
            "conductivity": cur_co,
            "wet_ceiling": None,
            "dry_floor": None,
            "dryness": None,
            "eta_days": None,
            "confidence": "uncalibrated",
            "status": "UNCALIBRATED",
            "slope_3d": None,
            "waterings_detected": 0,
            "valid_waterings": 0,
        }

        # Not enough history to calibrate at all.
        valid_means = [r["mean"] for r in mrows if r["mean"] is not None]
        if len(mrows) < 5 or len(valid_means) < 3:
            return result

        # ---- Detect watering events ----
        watering_events = self._detect_waterings(mrows, crows, jump_thresh)
        result["waterings_detected"] = len(watering_events)

        # No watering observed yet -> cannot calibrate. Stay uncalibrated
        # rather than fabricating a floor from percentiles of raw readings.
        if not watering_events:
            return result

        # ---- Wet ceiling: median of post-watering plateaus ----
        plateaus = [e["plateau"] for e in watering_events if e["plateau"] is not None]
        wet = statistics.median(plateaus) if plateaus else None
        result["wet_ceiling"] = round(wet, 1) if wet is not None else None

        if wet is None:
            return result

        # ---- Learned dry floor: driest pre-watering min among *valid* events ----
        # A watering is "valid" only if the plant had actually dried first
        # (pre-watering moisture < 65% of wet ceiling), filtering out
        # mass-waterings of still-moist plants.
        valid = [e for e in watering_events if e["pre_min"] is not None
                 and e["pre_min"] < self.valid_floor_frac * wet]
        result["valid_waterings"] = len(valid)

        # Without any valid dry-trigger watering, we have no learned floor.
        # Stay uncalibrated rather than guessing (the `low` tier is reserved
        # for a future weaker-floor estimate; see README).
        if not valid:
            return result
        learned_floor = min(e["pre_min"] for e in valid)

        # ---- Confidence tier ----
        if len(valid) >= self.conf_for_high:
            confidence = "high"
        elif len(valid) >= self.conf_for_medium:
            confidence = "medium"
        else:
            confidence = "low"
        result["confidence"] = confidence

        # ---- Dry floor: purely learned, no heuristic blend ----
        dry_floor = learned_floor
        result["dry_floor"] = round(dry_floor, 1)

        # ---- Dryness % (0 = just watered, 100 = water now) ----
        if cur_mo is not None and wet > dry_floor:
            need = max(0, min(100, round((wet - cur_mo) / (wet - dry_floor) * 100)))
        else:
            need = None
        result["dryness"] = need

        # ---- Slope (current dry-down cycle, capped at 3 days) and ETA ----
        # Only rows since the last detected watering reflect the current
        # dry-down; older rows would dilute the rate. Window = days since
        # the last watering, capped at 3 so the estimate stays responsive.
        last_event_d = dt.date.fromisoformat(watering_events[-1]["date"])
        last_row_d = dt.date.fromisoformat(mrows[-1]["t"])
        days_since = max(0, (last_row_d - last_event_d).days)
        slope = self._slope(mrows, n=min(days_since, 3))
        result["slope_3d"] = round(slope, 2) if slope is not None else None
        if slope is not None and slope < 0 and cur_mo is not None:
            days = (cur_mo - dry_floor) / (-slope)
            result["eta_days"] = round(max(0.0, days), 1)
        elif slope is not None and slope >= 0:
            result["eta_days"] = "rising"
        else:
            result["eta_days"] = "unknown"

        # ---- Status ----
        if need is None:
            result["status"] = "UNCALIBRATED"
        elif need >= 95:
            result["status"] = "WATER NOW"
        elif need >= 80:
            result["status"] = "water soon"
        else:
            result["status"] = "ok"

        return result

    def _detect_waterings(self, mrows, crows, jump_thresh):
        """Detect watering events from moisture jumps + slope reversal.

        Returns a list of dicts: {date, pre_min, plateau, pre_c, plateau_c}.
        """
        cby = {r["t"]: r for r in crows}
        events = []
        for i in range(1, len(mrows)):
            prev = mrows[i - 1]
            cur = mrows[i]
            if prev["mean"] is None or cur["mx"] is None:
                continue
            # Jump: today's max well above yesterday's mean.
            jump = cur["mx"] >= prev["mean"] + jump_thresh
            # Rate reversal: slope over days i-1..i exceeds the prior slope
            # by slope_reversal_margin (flattening/rising after draining).
            reversed_ = False
            if i >= 2:
                pre_slope = self._seg_slope(mrows, i - 2, i)      # days i-2..i-1
                post_slope = self._seg_slope(mrows, i - 1, i + 1)  # days i-1..i
                if pre_slope is not None and post_slope is not None:
                    reversed_ = post_slope > pre_slope + self.slope_reversal_margin
            if not (jump or reversed_):
                continue
            # Plateau = max daily mean in days i+1..i+4 (settled after drain).
            # With no settled post-watering readings we have no real plateau;
            # leave None so it's excluded from the wet-ceiling median rather
            # than polluting it with the watering-day spike max.
            future = [r["mean"] for r in mrows[i + 1:i + 5] if r["mean"] is not None]
            plateau = max(future) if future else None
            # Conductivity pre/post (for future Pass B confirmation).
            pre_c = cby.get(prev["t"], {}).get("mn")
            cfut = [cby.get(r["t"], {}).get("mean") for r in mrows[i + 1:i + 5]]
            cfut = [x for x in cfut if x is not None and x > 0]
            plateau_c = max(cfut) if cfut else None
            events.append({
                "date": cur["t"],
                "pre_min": prev["mn"],
                "plateau": plateau,
                "pre_c": pre_c,
                "plateau_c": plateau_c,
            })
        return events

    def _seg_slope(self, rows, a, b):
        """Least-squares slope of `mean` over rows[max(0,a):b]. Needs >=2 pts."""
        ys = [r["mean"] for r in rows[max(0, a):b] if r["mean"] is not None]
        return self._linear_slope(ys) if len(ys) >= 2 else None

    def _slope(self, rows, n=7):
        """Least-squares slope of `mean` over the last n rows. Needs >=3 pts."""
        if n < 1:
            return None
        ys = [r["mean"] for r in rows[-n:] if r["mean"] is not None]
        return self._linear_slope(ys) if len(ys) >= 3 else None

    @staticmethod
    def _linear_slope(ys):
        """Least-squares slope of ys vs integer index, or None if degenerate."""
        if len(ys) < 2:
            return None
        xs = list(range(len(ys)))
        mx = statistics.mean(xs)
        my = statistics.mean(ys)
        num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        den = sum((x - mx) ** 2 for x in xs)
        return num / den if den else None

    # ---- MQTT publish -------------------------------------------------------

    @staticmethod
    def _metric_specs():
        """Per-entity MQTT discovery spec. Entities with `stat_cla` are
        numeric (state must be a number or empty → unavailable); entities
        without it are free-form strings (status, confidence)."""
        return [
            {"obj": "dryness", "name": "Dryness", "key": "dryness",
             "dev_cla": "moisture", "unit": "%", "stat_cla": "measurement"},
            {"obj": "moisture", "name": "Moisture", "key": "moisture",
             "dev_cla": "moisture", "unit": "%", "stat_cla": "measurement"},
            {"obj": "conductivity", "name": "Conductivity", "key": "conductivity",
             "dev_cla": "conductivity", "unit": "µS/cm", "stat_cla": "measurement"},
            {"obj": "next_watering_estimate", "name": "Next Watering Estimate",
             "key": "eta_days", "dev_cla": "duration", "unit": "d",
             "stat_cla": "measurement"},
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

    def _plant_mqtt_payloads(self, plant_key, result):
        """Build the (topic, payload, retain) messages for one plant: one
        retained discovery config + one retained state per metric. Pure — no
        I/O — so it's unit-testable without a broker.

        Discovery is published every run; it's idempotent (HA ignores
        re-publication) and survives broker restarts where retention was lost.
        """
        dev = {
            "identifiers": ["drydown_%s" % plant_key],
            "name": plant_key.replace("_", " ").title(),
            "manufacturer": "drydown",
            "model": "Plant",
        }
        msgs = []
        for spec in self._metric_specs():
            obj = spec["obj"]
            disc_topic = "homeassistant/sensor/drydown/%s_%s/config" % (plant_key, obj)
            state_topic = "drydown/%s/%s/state" % (plant_key, obj)
            config = {
                "name": spec["name"],
                "uniq_id": "drydown_%s_%s" % (plant_key, obj),
                "stat_t": state_topic,
                "dev": dev,
            }
            for ck, cv in (("dev_cla", "dev_cla"), ("unit", "unit_of_meas"),
                           ("stat_cla", "stat_cla"), ("icon", "icon")):
                if ck in spec:
                    config[cv] = spec[ck]
            msgs.append((disc_topic, json.dumps(config), True))

            value = result.get(spec["key"])
            numeric = "stat_cla" in spec
            if numeric:
                # A unit-bearing entity must publish a number; non-numeric
                # (e.g. ETA "rising"/"unknown") or None -> empty (unavailable).
                ok = isinstance(value, (int, float)) and not isinstance(value, bool)
                payload = str(value) if ok else ""
            else:
                payload = "" if value is None else str(value)
            msgs.append((state_topic, payload, True))
        return msgs

    def _publish_plant(self, plant_key, result):
        if self.dry_run:
            self.log("DRYRUN_RESULT %s",
                     json.dumps(result, default=str, sort_keys=True))
            return
        for topic, payload, retain in self._plant_mqtt_payloads(plant_key, result):
            self._mqtt_publish(topic, payload, retain=retain)
        self.log("drydown %s: dryness=%s wet=%s dry=%s conf=%s eta=%s",
                 plant_key, result["dryness"], result["wet_ceiling"],
                 result["dry_floor"], result["confidence"], result["eta_days"])

    def _mqtt_connect(self):
        # Kept as a no-op stub for backward compatibility; publishing now goes
        # through HA's mqtt.publish service (see _mqtt_publish), which needs no
        # broker connection or credentials of its own.
        return

    def _mqtt_publish(self, topic, payload, retain=False):
        """Publish via HA's mqtt.publish service over the websocket.

        HA's MQTT integration is already authenticated to the broker, so the
        app holds no broker credentials — it asks HA to publish on its behalf.
        Discovery (retained) and state both go through this path.
        """
        self.call_service("mqtt/publish", topic=topic, payload=payload,
                          retain=retain)
