# drydown

![CI](https://github.com/satiani/drydown/actions/workflows/ci.yml/badge.svg)

A [Home Assistant](https://www.home-assistant.io/) / [AppDaemon](https://appdaemon.readthedocs.io/) app built for Xiaomi-style BLE plant sensors (Mi Flora / HHCC and similar) that expose moisture and conductivity entities. It turns those raw readings into a normalized, per-plant **dryness %** — calibrated from each plant's own watering history, so the same % means the same thing across sensors with different soils, probes, and pot sizes.

It reads moisture/conductivity history from InfluxDB and publishes one HA **device** per plant via [MQTT discovery](https://www.home-assistant.io/integrations/mqtt/#discovery).

## What you get per plant

One device (e.g. "Plant 4 Drydown") with a sensor entity per metric, each with its own history:

| entity | unit | what |
|---|---|---|
| Dryness | % | 0 = just watered, 100 = water now (the primary output) |
| Moisture / Conductivity | % / µS/cm | current raw readings |
| Wet Ceiling / Dry Floor | % | learned calibration bounds |
| Drydown rate (3d) | %/d | current dry-down rate |
| Status | — | ok / water soon / WATER NOW / UNCALIBRATED |
| Confidence | — | uncalibrated / medium / high |
| Waterings Detected / Valid Waterings | — | all waterings found / those used to calibrate the floor |

## How it works

The app detects watering events from moisture jumps and learns each plant's **wet ceiling** (post-watering plateau) and **dry floor** (driest reading before a real watering). Dryness is then where the current reading sits between those two bounds. The floor is *learned only* — a plant stays `UNCALIBRATED` until it has seen at least one watering where it had genuinely dried out first.

## Requirements

1. **InfluxDB** with your moisture/conductivity history (the HA InfluxDB integration).
2. **AppDaemon** add-on.
3. HA's **MQTT integration** with a configured broker.

## Install

1. Copy `apps/drydown/` into your AppDaemon apps dir (e.g. `/addon_configs/a0d7b954_appdaemon/apps/drydown/`).
2. In the AppDaemon add-on's Configuration panel, add `requests` to `python_packages` (see [`appdaemon_config.yaml`](appdaemon_config.yaml)) — the app needs it to query InfluxDB.
3. Edit `apps/drydown/drydown.yaml` to point at your InfluxDB and list your sensors (each needs a `moisture_entity`; the conductivity entity is derived by swapping `_moisture` for `_conductivity`).
4. Restart the AppDaemon add-on. Devices appear ~30s after startup.
5. *(Optional)* For a manual "run now" button in your dashboard, add the script + button card from [`homeassistant/manual_trigger.yaml`](homeassistant/manual_trigger.yaml) (see [Manual trigger](#manual-trigger) below).

For updates, [`scripts/deploy.sh`](scripts/deploy.sh) lints, tests, copies the modules over SSH, restarts the add-on, and verifies a clean run. It never overwrites your live `drydown.yaml`; SSH/host settings are overridable via env vars (see the script header).

## Configure

All options live in [`apps/drydown/drydown.yaml`](apps/drydown/drydown.yaml) (InfluxDB connection, sensors, and detection/confidence tunables) — each is commented. Set `dry_run: true` to compute and log results without publishing.

InfluxDB credentials live in plaintext in `drydown.yaml`. With the default add-on setup these are the non-secret in-network `homeassistant/homeassistant` credentials; if you set real ones, treat the file as a secret.

## Manual trigger

The app runs once ~30s after startup and then hourly. To trigger a run on demand, it also listens for a custom `drydown_run` event on HA's event bus. Fire that event and the app recomputes + republishes immediately.

The quickest way to fire it is **Developer Tools → Events → Fire Event** with event type `drydown_run`. For a clickable **dashboard button**, use the script + Button card in [`homeassistant/manual_trigger.yaml`](homeassistant/manual_trigger.yaml): the script fires the event and the button calls the script.

> Note: AppDaemon's `register_service` services are *not* exposed in HA's UI, so the trigger goes through HA's event bus rather than a HA service.

## Develop

```
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest && .venv/bin/ruff check .
```

The code under `apps/drydown/` is split by responsibility: `calibration.py` (pure math), `influx.py` (InfluxDB reads), `publish.py` (MQTT payloads), and `drydown.py` (the AppDaemon shell). Tests mirror that layout. `appdaemon` isn't installed in dev/CI, so `tests/conftest.py` injects a minimal stub; InfluxDB is exercised per-test by monkeypatching `requests` (or swapping `client.query`).

## License

MIT
