# drydown

A [Home Assistant](https://www.home-assistant.io/) / [AppDaemon](https://appdaemon.readthedocs.io/) app that turns raw plant-moisture readings into a normalized, per-plant **dryness %** — calibrated from each plant's own watering history, so the same % means the same thing across sensors with different soils, probes, and pot sizes.

It reads moisture/conductivity history from InfluxDB and publishes one HA **device** per plant via [MQTT discovery](https://www.home-assistant.io/integrations/mqtt/#discovery). It holds no broker credentials — it publishes through HA's `mqtt.publish` service.

## What you get per plant

One device (e.g. "Plant 4 Drydown") with a sensor entity per metric, each with its own history:

| entity | unit | what |
|---|---|---|
| Dryness | % | 0 = just watered, 100 = water now (the primary output) |
| Moisture / Conductivity | % / µS/cm | current raw readings |
| Next Watering Estimate | d | days until water needed |
| Wet Ceiling / Dry Floor | % | learned calibration bounds |
| Drydown rate (3d) | %/d | current dry-down rate |
| Status | — | ok / water soon / WATER NOW / UNCALIBRATED |
| Confidence | — | uncalibrated / low / medium / high |
| Waterings / Valid Waterings | — | watering events learned from |

## How it works

The app detects watering events from moisture jumps and learns each plant's **wet ceiling** (post-watering plateau) and **dry floor** (driest reading before a real watering). Dryness is then where the current reading sits between those two bounds. The floor is *learned only* — a plant stays `UNCALIBRATED` until it has seen at least one watering where it had genuinely dried out first.

## Requirements

1. **InfluxDB** with your moisture/conductivity history (the HA InfluxDB integration).
2. **AppDaemon** add-on.
3. **Mosquitto broker** add-on + HA's **MQTT integration** (the app relays through it, so it needs no broker credentials of its own).

## Install

1. Copy `apps/drydown/` into your AppDaemon apps dir (e.g. `/addon_configs/a0d7b954_appdaemon/apps/drydown/`).
2. Edit `apps/drydown/drydown.yaml` to point at your InfluxDB and list your sensors (each with a `pot_size`).
3. Restart the AppDaemon add-on. Devices appear ~30s after startup.

For updates, [`scripts/deploy.sh`](scripts/deploy.sh) lints, tests, copies the modules over SSH, restarts the add-on, and verifies a clean run. It never overwrites your live `drydown.yaml`; SSH/host settings are overridable via env vars (see the script header).

## Configure

All options live in [`apps/drydown/drydown.yaml`](apps/drydown/drydown.yaml) (InfluxDB connection, sensors, and detection/confidence tunables) — each is commented. Set `dry_run: true` to compute and log results without publishing.

InfluxDB credentials live in plaintext in `drydown.yaml`. With the default add-on setup these are the non-secret in-network `homeassistant/homeassistant` credentials; if you set real ones, treat the file as a secret.

## Develop

```
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest && .venv/bin/ruff check .
```

The code under `apps/drydown/` is split by responsibility: `calibration.py` (pure math), `influx.py` (InfluxDB reads), `publish.py` (MQTT payloads), and `drydown.py` (the AppDaemon shell). Tests mirror that layout; AppDaemon and InfluxDB are stubbed in `tests/conftest.py`.

## License

MIT
