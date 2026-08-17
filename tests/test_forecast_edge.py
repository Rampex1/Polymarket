"""The forward-test harness — its integrity rules, which are the whole point.

A retrieval-capable forecaster scored against already-resolved markets returns
a Brier skill near +1.0 and is indistinguishable from alpha. The only defence
is that the prediction is written down while the answer does not yet exist, so
these tests pin the two rules that enforce it: predictions are write-once, and
refused once the market has closed.
"""

import json

import pytest

from algorithms.forecast_edge import harness


@pytest.fixture
def conn(tmp_path):
    return harness.connect(str(tmp_path / "fc.db"))


def _ask_row(conn, market_id="0xm1", price=0.30, run="run1"):
    with conn:
        conn.execute(
            "INSERT INTO forecasts (market_id, run_id, asked_at, question,"
            " criteria, end_date, market_price, fee_rate, volume)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (market_id, run, 1_000, "Will X happen?", "", "2026-09-01",
             price, 0.0, 50_000.0))


def _preds(tmp_path, rows, run="run1"):
    p = tmp_path / "preds.json"
    p.write_text(json.dumps({"run_id": run, "predictions": rows}))
    return str(p)


# ── rule 1: never record against a closed market ─────────────────────────────

def test_a_closed_market_is_refused(conn, tmp_path, monkeypatch):
    """Recording after resolution is a backtest wearing a forward test's
    clothes — and it is exactly what a contaminated forecaster would do."""
    _ask_row(conn)
    monkeypatch.setattr(harness.api, "fetch_market_resolution",
                        lambda mid: {"closed": True})
    n = harness.record(conn, _preds(tmp_path, [{"market_id": "0xm1", "p": 0.9}]), "m")
    assert n == 0
    assert conn.execute("SELECT model_p FROM forecasts").fetchone()["model_p"] is None


def test_an_open_market_is_accepted(conn, tmp_path, monkeypatch):
    _ask_row(conn)
    monkeypatch.setattr(harness.api, "fetch_market_resolution",
                        lambda mid: {"closed": False})
    assert harness.record(conn, _preds(tmp_path, [{"market_id": "0xm1", "p": 0.9}]), "m") == 1
    assert conn.execute("SELECT model_p FROM forecasts").fetchone()["model_p"] == 0.9


# ── rule 2: write-once ───────────────────────────────────────────────────────

def test_a_prediction_cannot_be_revised(conn, tmp_path, monkeypatch):
    """A forecast that can be edited after the fact is not a forecast."""
    _ask_row(conn)
    monkeypatch.setattr(harness.api, "fetch_market_resolution",
                        lambda mid: {"closed": False})
    harness.record(conn, _preds(tmp_path, [{"market_id": "0xm1", "p": 0.20}]), "m")
    harness.record(conn, _preds(tmp_path, [{"market_id": "0xm1", "p": 0.95}]), "m")
    assert conn.execute("SELECT model_p FROM forecasts").fetchone()["model_p"] == 0.20


def test_an_unasked_market_is_refused(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(harness.api, "fetch_market_resolution",
                        lambda mid: {"closed": False})
    assert harness.record(conn, _preds(tmp_path, [{"market_id": "0xnope", "p": 0.5}]), "m") == 0


# ── universe selection ───────────────────────────────────────────────────────

@pytest.mark.parametrize("market, ok", [
    ({"question": "Will X resign?", "endDate": "2026-09-01T00:00:00Z",
      "volumeNum": 50_000}, True),
    ({"question": "Bitcoin Up or Down - August 17", "endDate": "2026-09-01T00:00:00Z",
      "volumeNum": 50_000}, False),                       # high-frequency coinflip
    ({"question": "Will A win?", "endDate": "2026-09-01T00:00:00Z",
      "volumeNum": 50_000, "sportsMarketType": "moneyline"}, False),  # measured dead
    ({"question": "Will X resign?", "endDate": "2026-09-01T00:00:00Z",
      "volumeNum": 100}, False),                          # untraded -> phantom price
    ({"question": "Will X resign?", "endDate": "2026-09-01T00:00:00Z",
      "volumeNum": 50_000, "feesEnabled": True,
      "feeSchedule": {"rate": 0.05}}, False),             # fee eats the edge
])
def test_universe_filters(market, ok):
    import datetime as dt
    now = dt.datetime(2026, 8, 17, tzinfo=dt.timezone.utc).timestamp()
    assert harness._eligible(market, now, 3.0, 30.0, 10_000.0, 0.0) is ok


def test_the_price_is_the_mid_of_a_two_sided_book():
    assert harness._yes_price({"bestBid": "0.28", "bestAsk": "0.32"}) == 0.30
    assert harness._yes_price({"bestBid": "0", "bestAsk": "0.32"}) is None


# ── scoring ──────────────────────────────────────────────────────────────────

def test_score_prefers_the_market_when_the_forecaster_is_worse(conn, capsys):
    """Sanity on the benchmark: the comparison is against market_price, not 0.5."""
    with conn:
        for i, (price, p, outcome) in enumerate([
            (0.20, 0.80, 0.0), (0.15, 0.70, 0.0), (0.10, 0.60, 0.0),
        ]):
            conn.execute(
                "INSERT INTO forecasts (market_id, run_id, asked_at, question,"
                " market_price, model_p, outcome, fee_rate)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (f"0x{i}", "r", 1, "q", price, p, outcome, 0.0))
    harness.score(conn)
    out = capsys.readouterr().out
    assert "skill vs market" in out
    assert "-" in out.split("skill vs market")[1][:12], "should report negative skill"
