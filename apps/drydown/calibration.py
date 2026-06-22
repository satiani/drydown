"""Pure drydown calibration math — no AppDaemon, InfluxDB, or MQTT.

Given a plant's daily-aggregate moisture/conductivity history, this module
detects watering events, learns a per-plant wet ceiling and dry floor, and
computes the 0-100 dryness %, drydown slope, status, and confidence.

Everything here is deterministic and side-effect free, so it's unit-testable
without stubbing any I/O. The AppDaemon app (``drydown.py``) owns all I/O and
calls :func:`compute_plant` with rows it pulled from InfluxDB.
"""

from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass
from typing import Optional, Sequence, TypedDict


class DailyRow(TypedDict):
    """One day's aggregate for an entity, as produced by the InfluxDB layer."""

    t: str                      # UTC day, "YYYY-MM-DD"
    mn: Optional[float]         # min(value)
    mx: Optional[float]         # max(value)
    mean: Optional[float]       # mean(value)
    last: Optional[float]       # last(value) — also the day's "current" reading


@dataclass(frozen=True)
class CalibrationConfig:
    """Tunables for watering detection and calibration.

    Defaults match the documented behaviour; the AppDaemon app overrides them
    from user config. Thresholds previously hardcoded inline (calibration
    gate, plateau window, status cutoffs) are named here so intent is explicit
    and they're adjustable.
    """

    # A single jump threshold. Real waterings spike day-over-day peaks by
    # tens of %, while sensor noise is a few % at most, so one threshold
    # cleanly separates them — per-pot-size thresholds added knobs without
    # changing which events were detected (and the lowest one let noise in).
    jump_threshold: float = 10.0
    valid_floor_frac: float = 0.65
    conf_for_high: int = 3
    # Minimum history before any calibration is attempted.
    min_history_rows: int = 5
    min_valid_means: int = 3
    # Dryness % cutoffs for the Status entity.
    water_now_threshold: int = 95
    water_soon_threshold: int = 80


@dataclass
class WateringEvent:
    """A detected watering: the day it happened, the pre-watering driest
    reading, and the settled post-watering plateau (None if not yet settled)."""

    date: str
    pre_min: Optional[float]
    plateau: Optional[float]


@dataclass
class PlantResult:
    """Computed per-plant output. Numeric fields are None when unknown; the
    MQTT layer maps each field to one entity by attribute name."""

    plant: str
    moisture_entity: str
    moisture: Optional[float]
    conductivity: Optional[float]
    wet_ceiling: Optional[float] = None
    dry_floor: Optional[float] = None
    dryness: Optional[int] = None
    confidence: str = "uncalibrated"
    status: str = "UNCALIBRATED"
    slope_3d: Optional[float] = None
    waterings_detected: int = 0
    valid_waterings: int = 0


# ---- small numeric helpers -------------------------------------------------

def linear_slope_xy(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    """Least-squares slope of ys vs xs, or None if degenerate."""
    if len(ys) < 2:
        return None
    mx = statistics.mean(xs)
    my = statistics.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    return num / den if den else None


def linear_slope(ys: Sequence[float]) -> Optional[float]:
    """Least-squares slope of ys vs integer index, or None if degenerate."""
    return linear_slope_xy(list(range(len(ys))), ys)


def slope_over(rows: Sequence[DailyRow], n: int = 7) -> Optional[float]:
    """Least-squares slope of `mean` (%/day) over the last n reading-days,
    using each row's actual date as x so sensors that skip days don't distort
    the rate. Needs >=3 pts."""
    if n < 1:
        return None
    pts = [(dt.date.fromisoformat(r["t"]), r["mean"]) for r in rows[-n:]
           if r["mean"] is not None]
    if len(pts) < 3:
        return None
    xs = [d.toordinal() for d, _ in pts]
    ys = [y for _, y in pts]
    return linear_slope_xy(xs, ys)


def last_value(rows: Sequence[DailyRow]) -> Optional[float]:
    """Return the most recent non-null `last` selector across daily rows.

    The per-day `last` is the latest point in that day's bucket, so the most
    recent non-null one across the window is the entity's current reading —
    no separate last() query needed.
    """
    for r in reversed(rows):
        if r.get("last") is not None:
            return r["last"]
    return None


# ---- watering detection ----------------------------------------------------

def detect_waterings(rows: Sequence[DailyRow],
                     jump_thresh: float) -> list[WateringEvent]:
    """Detect watering events from moisture jumps."""
    events: list[WateringEvent] = []
    for i in range(1, len(rows)):
        prev = rows[i - 1]
        cur = rows[i]
        if prev["mx"] is None or cur["mx"] is None:
            continue
        # Jump: today's max well above yesterday's max (peak-to-peak). Comparing
        # against yesterday's *mean* double-counts a single watering: the
        # watering-day mean is a hybrid of pre-water dry readings and the pour
        # spike, so the settled plateau the *next* day clears it too and fires
        # again. Yesterday's max is the clean prior peak — nothing the
        # following day can exceed, so each watering registers exactly once.
        if cur["mx"] < prev["mx"] + jump_thresh:
            continue
        # Plateau = the next day's settled mean (the watering day's own mean is
        # a hybrid of pre-water and spike readings). A wider max-over-window
        # only ever reproduced this next-day value here, and could bleed into a
        # following watering's plateau — so just take the next day. None when
        # there's no settled day yet, so it's excluded from the wet-ceiling.
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        plateau = nxt["mean"] if nxt is not None else None
        events.append(WateringEvent(
            date=cur["t"], pre_min=prev["mn"], plateau=plateau))
    return events


# ---- top-level per-plant computation ---------------------------------------

def compute_plant(plant_key: str, moisture_entity: str,
                  mrows: Sequence[DailyRow], crows: Sequence[DailyRow],
                  config: CalibrationConfig) -> PlantResult:
    """Compute the full per-plant result from daily moisture/conductivity rows.

    Current readings are the latest non-null `last` of each series. A plant
    stays UNCALIBRATED until it has at least one *valid* (dry-trigger)
    watering; the dry floor is purely learned, never a heuristic prior.
    """
    cur_mo = last_value(mrows)
    cur_co = last_value(crows)

    result = PlantResult(
        plant=plant_key, moisture_entity=moisture_entity,
        moisture=cur_mo, conductivity=cur_co)

    # Not enough history to calibrate at all.
    valid_means = [r["mean"] for r in mrows if r["mean"] is not None]
    if len(mrows) < config.min_history_rows or len(valid_means) < config.min_valid_means:
        return result

    # ---- Detect watering events ----
    events = detect_waterings(mrows, config.jump_threshold)
    result.waterings_detected = len(events)

    # ---- Slope (current dry-down cycle, last 3 readings) ----
    # Computed before any calibration gating: the drydown rate is meaningful
    # on its own and useful for uncalibrated plants too (it needs only moisture
    # readings, not learned bounds). Uses rows since the last detected watering
    # when one exists, else the whole window.
    if events:
        last_event_d = dt.date.fromisoformat(events[-1].date)
        post = [r for r in mrows
                if dt.date.fromisoformat(r["t"]) > last_event_d]
    else:
        post = list(mrows)
    slope = slope_over(post, n=3)
    result.slope_3d = round(slope, 2) if slope is not None else None

    if not events:
        return result

    # ---- Wet ceiling: median of post-watering plateaus ----
    plateaus = [e.plateau for e in events if e.plateau is not None]
    wet = statistics.median(plateaus) if plateaus else None
    result.wet_ceiling = round(wet, 1) if wet is not None else None
    if wet is None:
        return result

    # ---- Learned dry floor: driest pre-watering min among *valid* events ----
    valid = [e for e in events if e.pre_min is not None
             and e.pre_min < config.valid_floor_frac * wet]
    result.valid_waterings = len(valid)
    if not valid:
        # No valid dry-trigger watering -> no learned floor. Stay uncalibrated
        # rather than guessing (the `low` tier is reserved for a future
        # weaker-floor estimate; see README).
        return result
    dry_floor = min(e.pre_min for e in valid)
    result.dry_floor = round(dry_floor, 1)

    # ---- Confidence tier ----
    # Calibrated by at least one valid watering (we returned above otherwise),
    # so it's `medium`, promoted to `high` once enough valid waterings agree.
    result.confidence = (
        "high" if len(valid) >= config.conf_for_high else "medium")

    # ---- Dryness % (0 = just watered, 100 = water now) ----
    if cur_mo is not None and wet > dry_floor:
        need = max(0, min(100, round((wet - cur_mo) / (wet - dry_floor) * 100)))
    else:
        need = None
    result.dryness = need

    # ---- Status ----
    if need is None:
        result.status = "UNCALIBRATED"
    elif need >= config.water_now_threshold:
        result.status = "WATER NOW"
    elif need >= config.water_soon_threshold:
        result.status = "water soon"
    else:
        result.status = "ok"

    return result
