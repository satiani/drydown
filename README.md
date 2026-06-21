# drydown

A [Home Assistant](https://www.home-assistant.io/) / [AppDaemon](https://appdaemon.readthedocs.io/) app that turns raw plant-moisture sensor readings into a normalized, per-plant water-need indicator — calibrated from each plant's own watering history so the same % means the same thing across sensors with different soils, probe placements, and pot sizes.

Each plant gets one `sensor.drydown_<plant>` entity: a 0–100 "how close to needing water" percentage, with an ETA in days and a confidence tier. The dry floor and wet ceiling are learned purely from detected watering events — no manual logging, no heuristic prior. A plant stays `uncalibrated` until it has seen at least one real dry-then-water cycle.

## Install

1. Copy `apps/drydown/` into your AppDaemon add-on's apps dir.
2. Tag each source moisture sensor with a `pot_size` attribute (`small` / `medium` / `large`) in HA's `customize` block — see [`configuration.yaml.example`](configuration.yaml.example).
3. Edit [`apps/drydown/drydown.yaml`](apps/drydown/drydown.yaml) to point at your InfluxDB and list your sensors.
4. Restart the AppDaemon add-on. Entities appear on the first run.

## Configure

Everything is in [`apps/drydown/drydown.yaml`](apps/drydown/drydown.yaml): InfluxDB connection + retry, sensors, lookback window, and the watering-detection / confidence tunables (all commented).

Set `dry_run: true` to compute and log results per plant without writing any entities — useful for validating calibration against live data before enabling for real.

## Develop

```
python -m venv .venv && .venv/bin/pip install pytest requests
.venv/bin/python -m pytest
```

Tests cover the calibration math, watering detection, and InfluxDB layer; AppDaemon is stubbed (`tests/conftest.py`).

## License

MIT
