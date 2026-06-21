"""InfluxDB read layer for drydown.

A thin client over InfluxDB's HTTP `/query` API plus a helper that pulls daily
moisture/conductivity aggregates for a set of entities. Isolated from
AppDaemon so it can be tested by patching ``requests``; the only dependency on
the app is a ``log`` callable matching AppDaemon's ``self.log`` signature.

Credentials are sent via HTTP Basic auth (a header), not URL query params, so
they don't land in InfluxDB's request log. String literals interpolated into
InfluxQL are escaped to avoid malformed queries from unusual entity ids.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

import requests

# Matches AppDaemon's self.log(msg, *args, level=...).
LogFn = Callable[..., None]


@dataclass(frozen=True)
class InfluxConfig:
    host: str
    port: int
    database: str
    username: str
    password: str
    # Raw FROM-clause tokens: a quoted literal (e.g. '"%"') or a /regex/. The
    # HA InfluxDB integration names measurements by unit_of_measurement.
    moisture_measurement: str = '"%"'
    conductivity_measurement: str = "/.*S.cm/"
    retries: int = 3
    backoff: float = 1.0


def escape_literal(value: str) -> str:
    """Escape a string for use inside an InfluxQL single-quoted literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def bare_eid(entity_id: str) -> str:
    """Return entity_id without its HA domain prefix.

    HA's InfluxDB integration stores `domain` and `entity_id` as separate
    tags, so the entity_id tag is the bare id (e.g. 'plant_1_moisture', not
    'sensor.plant_1_moisture'). We match on the bare id when querying.
    """
    return entity_id.split(".", 1)[1] if "." in entity_id else entity_id


def entity_filter(ids: Iterable[str]) -> str:
    """Build an OR clause of entity_id filters matching bare OR full id."""
    clauses = []
    for e in ids:
        b = bare_eid(e)
        if b == e:
            clauses.append(f'"entity_id" = \'{escape_literal(e)}\'')
        else:
            clauses.append(
                f'("entity_id" = \'{escape_literal(b)}\' '
                f'OR "entity_id" = \'{escape_literal(e)}\')')
    return " OR ".join(clauses)


class InfluxClient:
    """Minimal InfluxDB HTTP query client with bounded retry."""

    def __init__(self, config: InfluxConfig, log: LogFn) -> None:
        self._cfg = config
        self._log = log

    def query(self, q: str) -> dict:
        """Run a query and return its first result object ({} on error)."""
        url = "http://%s:%s/query" % (self._cfg.host, self._cfg.port)
        params = {"db": self._cfg.database, "q": q}
        r = self._get_with_retry(url, params)
        if r is None:
            return {}
        data = r.json()
        results = data.get("results", [{}])
        result = results[0] if results else {}
        if "error" in result:
            self._log("drydown: InfluxDB query error: %s",
                      result["error"], level="ERROR")
            return {}
        return result

    def _get_with_retry(self, url: str, params: dict) -> Optional[requests.Response]:
        """GET with bounded exponential backoff on transient failures.

        Retries connection errors, timeouts, and 5xx. A 4xx (bad query) is not
        retried — it won't fix itself. Returns the Response or None.

        NOTE: AppDaemon runs callbacks in a worker-thread pool, so the
        time.sleep below blocks one worker thread, not the whole app. With the
        default 3 attempts the worst case is ~3s; acceptable at this cadence.
        """
        last_exc: Optional[Exception] = None
        auth = (self._cfg.username, self._cfg.password)
        for attempt in range(self._cfg.retries):
            try:
                r = requests.get(url, params=params, auth=auth, timeout=30)
                r.raise_for_status()
                return r
            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
            except requests.HTTPError as e:
                if e.response is not None and 500 <= e.response.status_code < 600:
                    last_exc = e
                else:
                    self._log("drydown: InfluxDB client error %s: %s",
                              e.response.status_code if e.response else "?",
                              e, level="ERROR")
                    return None
            if attempt < self._cfg.retries - 1:
                wait = self._cfg.backoff * (2 ** attempt)
                self._log("drydown: InfluxDB request failed (attempt %d/%d): %s; "
                          "retrying in %.1fs", attempt + 1, self._cfg.retries,
                          last_exc, wait, level="WARNING")
                time.sleep(wait)
        self._log("drydown: InfluxDB request gave up after %d attempts: %s",
                  self._cfg.retries, last_exc, level="ERROR")
        return None


def pull_daily_history(client: InfluxClient, entity_ids: list[str],
                       measurement: str, since: str) -> dict[str, list[dict]]:
    """Return {full_entity_id: [daily rows]} for one measurement.

    Daily aggregates (min/max/mean) plus the per-day last() — which also serves
    as the "current" reading — over the window, in one batched query. Results
    are re-keyed from InfluxDB's bare entity_id tag back to the caller's full
    config id.

    Time math is UTC: `since` is a UTC date and InfluxDB's time(1d) buckets
    are UTC by default, so day boundaries are deterministic regardless of tz.
    """
    if not entity_ids:
        return {}
    ors = entity_filter(entity_ids)
    q = (f'SELECT min("value"), max("value"), mean("value"), last("value") '
         f'FROM {measurement} WHERE ({ors}) AND time >= \'{escape_literal(since)}\' '
         f'GROUP BY time(1d), "entity_id" fill(null)')
    data = client.query(q)
    bare_to_full = {bare_eid(e): e for e in entity_ids}
    out: dict[str, list[dict]] = {}
    for series in data.get("series", []):
        eid_tag = series.get("tags", {}).get("entity_id")
        eid = bare_to_full.get(eid_tag, eid_tag)
        cols = series["columns"]
        rows = []
        for v in series["values"]:
            rec = dict(zip(cols, v))
            if rec.get("min") is None:
                continue
            rows.append({
                # time(1d) buckets -> one row per UTC day, so [:10] strips the
                # identical T00:00:00Z suffix.
                "t": rec["time"][:10],
                "mn": rec["min"],
                "mx": rec["max"],
                "mean": rec["mean"],
                "last": rec["last"],
            })
        out.setdefault(eid, []).extend(rows)
    return out
