"""Tests for the InfluxDB read layer: query building/escaping, the bare-tag
re-keying, Basic-auth credential handling, and retry semantics."""

from __future__ import annotations

import conftest  # noqa: F401  (installs the appdaemon stub at import time)
import influx


def make_client(**over):
    over.setdefault("retries", 3)
    cfg = influx.InfluxConfig(host="h", port=8086, database="db",
                              username="u", password="p", backoff=0, **over)
    logs = []

    def log(msg, *a, level="INFO", **k):
        logs.append((level, msg % a if a else msg))

    client = influx.InfluxClient(cfg, log)
    client.logs = logs  # type: ignore[attr-defined]
    return client


# ---- bare_eid / entity_filter / escaping -----------------------------------

def test_bare_eid_strips_domain():
    assert influx.bare_eid("sensor.plant_1_moisture") == "plant_1_moisture"
    assert influx.bare_eid("plant_1_moisture") == "plant_1_moisture"
    assert influx.bare_eid("sensor.a.b_moisture") == "a.b_moisture"


def test_entity_filter_matches_bare_or_full():
    f = influx.entity_filter(["sensor.plant_1_moisture", "plant_2_moisture"])
    assert '"entity_id" = \'plant_1_moisture\'' in f
    assert '"entity_id" = \'sensor.plant_1_moisture\'' in f
    assert '"entity_id" = \'plant_2_moisture\'' in f
    assert " OR " in f


def test_entity_filter_escapes_single_quotes():
    """An id containing a single quote must be escaped, not allowed to break
    out of the InfluxQL string literal."""
    f = influx.entity_filter(["sensor.o'brien_moisture"])
    assert "o\\'brien" in f
    # The raw, unescaped quote sequence must not appear.
    assert "o'brien_moisture'" not in f.replace("\\'", "")


def test_escape_literal_handles_backslash_and_quote():
    assert influx.escape_literal("a'b") == "a\\'b"
    assert influx.escape_literal("a\\b") == "a\\\\b"


# ---- pull_daily_history ----------------------------------------------------

def test_pull_history_keys_by_full_id_for_bare_tags():
    """HA's InfluxDB stores entity_id WITHOUT the domain prefix. pull must
    filter on the bare id and re-key the result by the full config id."""
    client = make_client()

    def fake_query(q):
        return {"series": [{
            "tags": {"entity_id": "plant_1_moisture", "domain": "sensor"},
            "columns": ["time", "min", "max", "mean", "last"],
            "values": [
                ["2024-01-01T00:00:00Z", 10, 20, 15, 18],
                ["2024-01-02T00:00:00Z", 12, 22, 17, 22],
            ],
        }]}

    client.query = fake_query  # type: ignore[assignment]
    out = influx.pull_daily_history(
        client, ["sensor.plant_1_moisture"], '"%"', "2024-01-01")
    assert "sensor.plant_1_moisture" in out
    assert "plant_1_moisture" not in out
    assert len(out["sensor.plant_1_moisture"]) == 2
    assert out["sensor.plant_1_moisture"][0]["t"] == "2024-01-01"
    assert out["sensor.plant_1_moisture"][1]["last"] == 22


def test_pull_history_empty_ids_short_circuits():
    client = make_client()
    called = {"n": 0}

    def fake_query(q):
        called["n"] += 1
        return {}

    client.query = fake_query  # type: ignore[assignment]
    assert influx.pull_daily_history(client, [], '"%"', "2024-01-01") == {}
    assert called["n"] == 0  # no query issued for an empty id list


def test_pull_history_skips_null_min_rows():
    client = make_client()
    client.query = lambda q: {"series": [{  # type: ignore[assignment]
        "tags": {"entity_id": "plant_1_moisture"},
        "columns": ["time", "min", "max", "mean", "last"],
        "values": [
            ["2024-01-01T00:00:00Z", None, None, None, None],
            ["2024-01-02T00:00:00Z", 12, 22, 17, 22],
        ],
    }]}
    out = influx.pull_daily_history(
        client, ["sensor.plant_1_moisture"], '"%"', "2024-01-01")
    assert len(out["sensor.plant_1_moisture"]) == 1
    assert out["sensor.plant_1_moisture"][0]["t"] == "2024-01-02"


# ---- query / retry ---------------------------------------------------------

def test_query_sends_basic_auth_not_url_creds(monkeypatch):
    """Credentials go in the Basic-auth header, never the query params."""
    seen = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": [{"series": []}]}

    def fake_get(url, params=None, auth=None, timeout=None):
        seen["params"] = params
        seen["auth"] = auth
        return FakeResp()

    monkeypatch.setattr(influx.requests, "get", fake_get)
    out = make_client().query("SELECT 1")
    assert out == {"series": []}
    assert seen["auth"] == ("u", "p")
    assert "p" not in seen["params"] and "u" not in seen["params"]
    assert set(seen["params"]) == {"db", "q"}


def test_query_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": [{"series": []}]}

    def fake_get(url, params=None, auth=None, timeout=None):
        calls["n"] += 1
        if calls["n"] < 2:
            raise influx.requests.ConnectionError("boom")
        return FakeResp()

    monkeypatch.setattr(influx.requests, "get", fake_get)
    assert make_client().query("SELECT 1") == {"series": []}
    assert calls["n"] == 2


def test_query_4xx_not_retried(monkeypatch):
    calls = {"n": 0}

    class FakeResp:
        status_code = 400

    class FakeHTTPError(influx.requests.HTTPError):
        def __init__(self):
            super().__init__()
            self.response = FakeResp()

    def fake_get(url, params=None, auth=None, timeout=None):
        calls["n"] += 1
        raise FakeHTTPError()

    monkeypatch.setattr(influx.requests, "get", fake_get)
    assert make_client().query("SELECT 1") == {}
    assert calls["n"] == 1


def test_query_5xx_retried_then_gives_up(monkeypatch):
    calls = {"n": 0}

    class FakeResp:
        status_code = 503

    class FakeHTTPError(influx.requests.HTTPError):
        def __init__(self):
            super().__init__()
            self.response = FakeResp()

    def fake_get(url, params=None, auth=None, timeout=None):
        calls["n"] += 1
        raise FakeHTTPError()

    monkeypatch.setattr(influx.requests, "get", fake_get)
    assert make_client(retries=3).query("SELECT 1") == {}
    assert calls["n"] == 3


def test_query_reports_influx_error(monkeypatch):
    client = make_client()

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": [{"error": "bad query"}]}

    monkeypatch.setattr(influx.requests, "get",
                        lambda *a, **k: FakeResp())
    assert client.query("SELECT bogus") == {}
    assert any("bad query" in m for _, m in client.logs)
