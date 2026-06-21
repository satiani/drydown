# drydown

A [Home Assistant](https://www.home-assistant.io/) / [AppDaemon](https://appdaemon.readthedocs.io/) app that turns raw plant-moisture sensor readings into a normalized, per-plant **dryness** indicator — calibrated from each plant's own watering history so the same % means the same thing across sensors with different soils, probe placements, and pot sizes.

The app is a pure **InfluxDB → MQTT** transformer: it reads moisture/conductivity history from InfluxDB, learns a per-plant wet ceiling and dry floor from detected watering events, and publishes one Home Assistant **device** per plant over MQTT (via [MQTT discovery](https://www.home-assistant.io/integrations/mqtt/#discovery)) with one sensor entity per metric. It reads nothing from HA's state machine and publishes via HA's `mqtt.publish` service — so it holds no broker credentials. Each entity gets its own history; HA's MQTT integration owns the entity/device registries.

## What you get per plant

One device (e.g. "Plant 4") with these sensor entities, each with a sensible icon, unit, and full history:

| entity | unit | what |
|---|---|---|
| Dryness | % | 0 = just watered, 100 = water now (the primary output) |
| Moisture | % | current raw reading |
| Conductivity | µS/cm | current raw reading |
| Next Watering Estimate | d | days until water needed (unavailable when it can't be estimated) |
| Wet Ceiling | % | learned field-capacity plateau |
| Dry Floor | % | learned trigger floor |
| Drydown rate (3d) | %/d | moisture dry-down rate |
| Status | — | ok / water soon / WATER NOW / UNCALIBRATED |
| Confidence | — | uncalibrated / low / medium / high |
| Waterings Detected | — | count of watering events learned from |
| Valid Waterings | — | count that qualified as dry-trigger waterings |

## How it works

The dry floor is **purely learned** from detected watering events — no heuristic prior. A plant stays `UNCALIBRATED` until it has seen at least one *valid* watering (one where the plant had actually dried first).

- **Wet ceiling** — median of post-watering plateaus across detected events.
- **Dry floor** — the driest pre-watering moisture among *valid* waterings. A watering is valid only if pre-watering moisture was below 65% of the wet ceiling, filtering out mass-waterings of still-moist plants.
- **Watering detection** — from the moisture reading alone: a watering is detected on a day where the max moisture jumps ≥ `jump_threshold` above the previous day's mean **or** the dry-down slope reverses. The threshold is adaptive by pot size (small 15, medium 8, large 5).
- **Dryness** — `clamp((wet_ceiling − current) / (wet_ceiling − dry_floor) × 100, 0, 100)`.
- **Next Watering Estimate** — `(current − dry_floor) / −slope_3d`.

## Requirements

1. **InfluxDB** with your moisture/conductivity history (the HA InfluxDB integration). The app queries measurements named by unit (`"%"`, `/.*S.cm/`) — configurable.
2. **AppDaemon** add-on.
3. **Mosquitto broker** add-on + HA's **MQTT integration**. The app publishes discovery + state by calling HA's `mqtt.publish` service, so it needs no broker credentials of its own — HA's MQTT integration (already authenticated to the broker) relays the messages.

## Install

1. Copy `apps/drydown/` into your AppDaemon add-on's apps dir (`/addon_configs/a0d7b954_appdaemon/apps/drydown/`).
2. Edit `apps/drydown/drydown.yaml` to point at your InfluxDB and list your sensors (with `pot_size`).
3. Ensure the Mosquitto broker add-on is running and HA's MQTT integration is configured (see Requirements).
4. Restart the AppDaemon add-on. Devices appear on the first run (~30s after startup).

## Configure

Everything is in [`apps/drydown/drydown.yaml`](apps/drydown/drydown.yaml): InfluxDB connection + retry, sensors (with `pot_size`), lookback window, and the watering-detection / confidence tunables (all commented). No broker credentials are needed — the app publishes through HA's `mqtt.publish` service.

Set `dry_run: true` to compute and log results per plant without publishing — useful for validating calibration against live data with no side effects.

## Develop

```
python -m venv .venv && .venv/bin/pip install pytest requests
.venv/bin/python -m pytest
```

Tests cover the calibration math, watering detection, InfluxDB layer, and MQTT payload construction. AppDaemon and InfluxDB are stubbed (`tests/conftest.py`).

## License

MIT
