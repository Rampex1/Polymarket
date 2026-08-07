"""
Price-history archiver tests.

The archiver hoards CLOB price history into its own SQLite file because the
public API drops history once markets resolve. Real SQLite via tmp_path,
HTTP stubbed at the bot.fetcher boundary (the archiver's only network path).
"""

import json

import pytest

from tests.conftest import make_global_trade


@pytest.fixture
def conn(tmp_path):
    from discovery import archive

    c = archive.connect(str(tmp_path / "arch.db"))
    yield c
    c.close()


# ---------------------------------------------------------------------------
# data/ directory — default path + auto-created parents
# ---------------------------------------------------------------------------


def test_connect_creates_missing_parent_dirs(tmp_path):
    from discovery import archive

    nested = tmp_path / "data" / "deep" / "arch.db"
    c = archive.connect(str(nested))
    try:
        assert nested.exists()
    finally:
        c.close()


def test_default_db_path_prefers_data_dir(tmp_path, monkeypatch):
    import os

    from discovery import archive

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCOVERY_ARCHIVE_DB", raising=False)
    assert archive.default_db_path() == os.path.join("data", "discovery_archive.db")


def test_default_db_path_legacy_fallback(tmp_path, monkeypatch):
    from discovery import archive

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCOVERY_ARCHIVE_DB", raising=False)
    (tmp_path / "discovery_archive.db").touch()
    assert archive.default_db_path() == "discovery_archive.db"


def test_default_db_path_env_override_wins(tmp_path, monkeypatch):
    from discovery import archive

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DISCOVERY_ARCHIVE_DB", "/elsewhere/a.db")
    assert archive.default_db_path() == "/elsewhere/a.db"


# ---------------------------------------------------------------------------
# Storage primitives
# ---------------------------------------------------------------------------


def test_upsert_history_is_idempotent(conn):
    from discovery import archive

    pts = [{"t": 1, "p": 0.5}, {"t": 2, "p": 0.6}]
    assert archive.upsert_history(conn, "tok1", 60, pts) == 2
    assert archive.upsert_history(conn, "tok1", 60, pts) == 0     # re-insert
    # A different fidelity is a separate series, not a duplicate.
    assert archive.upsert_history(conn, "tok1", 1, pts) == 2

    n = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    assert n == 4


def test_track_market_registers_once_and_keeps_first_source(conn):
    from discovery import archive

    market = {
        "conditionId": "0xc1",
        "question": "Q?",
        "clobTokenIds": json.dumps(["t1", "t2"]),
    }
    archive.track_market(conn, market, source="gamma_active")
    archive.track_market(conn, market, source="whale_flow")      # re-track

    rows = conn.execute(
        "SELECT condition_id, token_ids, source FROM tracked_markets"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "0xc1"
    assert json.loads(rows[0][1]) == ["t1", "t2"]
    assert rows[0][2] == "gamma_active"


# ---------------------------------------------------------------------------
# Universe collection — union of active, recently-closed, whale-touched
# ---------------------------------------------------------------------------


def test_collect_universe_unions_and_dedupes_sources(conn, monkeypatch):
    from bot import fetcher
    from discovery import archive

    active = [
        {"conditionId": "0xa", "question": "A?", "clobTokenIds": '["ta1","ta2"]'},
    ]
    recently_closed = [
        {"conditionId": "0xb", "question": "B?", "clobTokenIds": '["tb1","tb2"]'},
        {"conditionId": "0xa", "question": "A?", "clobTokenIds": '["ta1","ta2"]'},
    ]

    def fake_top_markets(closed, limit=50, end_date_min=None, end_date_max=None):
        return recently_closed if closed else active

    monkeypatch.setattr(fetcher, "fetch_top_markets", fake_top_markets)
    monkeypatch.setattr(
        fetcher, "fetch_global_trades",
        lambda *a, **kw: [make_global_trade(market_id="0xw", asset_id="tw1")],
    )
    # Whale-flow markets get their full token list from Gamma — the firehose
    # row only carries the traded side.
    monkeypatch.setattr(
        fetcher, "fetch_market_resolution",
        lambda mid: {
            "conditionId": mid, "question": "W?",
            "clobTokenIds": '["tw1","tw2"]',
        },
    )

    added = archive.collect_universe(conn, min_cash=10_000.0, top_n=5)
    assert added == 3                                  # 0xa deduped

    rows = dict(conn.execute(
        "SELECT condition_id, token_ids FROM tracked_markets").fetchall())
    assert set(rows) == {"0xa", "0xb", "0xw"}
    # Both outcome tokens archived, not just the one the whale traded.
    assert json.loads(rows["0xw"]) == ["tw1", "tw2"]


def test_collect_universe_whale_flow_falls_back_to_traded_token(
    conn, monkeypatch
):
    """Gamma lookup failure must not drop the market — half a price series
    beats none."""
    from bot import fetcher
    from discovery import archive

    monkeypatch.setattr(fetcher, "fetch_top_markets", lambda *a, **kw: [])
    monkeypatch.setattr(
        fetcher, "fetch_global_trades",
        lambda *a, **kw: [make_global_trade(market_id="0xw", asset_id="tw1")],
    )
    monkeypatch.setattr(fetcher, "fetch_market_resolution", lambda mid: None)

    assert archive.collect_universe(conn) == 1
    rows = conn.execute(
        "SELECT condition_id, token_ids FROM tracked_markets").fetchall()
    assert rows == [("0xw", '["tw1"]')]


def test_collect_universe_skips_gamma_lookup_for_tracked_markets(
    conn, monkeypatch
):
    """An already-tracked whale market must not cost a Gamma round-trip on
    every pass — the firehose repeats hot markets constantly."""
    from bot import fetcher
    from discovery import archive

    archive.track_market(
        conn,
        {"conditionId": "0xw", "question": "W?", "clobTokenIds": '["tw1","tw2"]'},
        source="gamma_active",
    )

    monkeypatch.setattr(fetcher, "fetch_top_markets", lambda *a, **kw: [])
    monkeypatch.setattr(
        fetcher, "fetch_global_trades",
        lambda *a, **kw: [make_global_trade(market_id="0xw", asset_id="tw1")],
    )
    gamma_calls = []
    monkeypatch.setattr(
        fetcher, "fetch_market_resolution",
        lambda mid: gamma_calls.append(mid) or None,
    )

    assert archive.collect_universe(conn) == 0
    assert gamma_calls == []


# ---------------------------------------------------------------------------
# Snapshotting
# ---------------------------------------------------------------------------


def test_snapshot_all_stores_points_and_skips_failures(conn, monkeypatch):
    from bot import fetcher
    from discovery import archive

    archive.track_market(
        conn,
        {"conditionId": "0xc1", "question": "Q1", "clobTokenIds": '["t1"]'},
        source="test",
    )
    archive.track_market(
        conn,
        {"conditionId": "0xc2", "question": "Q2", "clobTokenIds": '["t2"]'},
        source="test",
    )

    def fake_history(token_id, fidelity=60, **kw):
        if token_id == "t1":
            return [{"t": 10, "p": 0.4}, {"t": 20, "p": 0.45}]
        return None                                    # HTTP failure for t2

    monkeypatch.setattr(fetcher, "fetch_price_history", fake_history)

    summary = archive.snapshot_all(conn, fidelity=60)
    assert summary == {"tokens": 2, "points_added": 2, "failures": 1}

    rows = conn.execute(
        "SELECT token_id, ts, price FROM price_history ORDER BY ts"
    ).fetchall()
    assert [(r[0], r[1]) for r in rows] == [("t1", 10), ("t1", 20)]

    # Successful snapshot stamps the market; the failed one stays unstamped.
    stamped = dict(conn.execute(
        "SELECT condition_id, last_snapshot FROM tracked_markets").fetchall())
    assert stamped["0xc1"] is not None
    assert stamped["0xc2"] is None


def test_snapshot_all_uses_delta_fetch_after_first_pass(conn, monkeypatch):
    """First pass pulls full history; later passes must fetch only the
    window since last_snapshot, or bandwidth grows without bound as the
    archive ages."""
    from bot import fetcher
    from discovery import archive

    archive.track_market(
        conn,
        {"conditionId": "0xc1", "question": "Q1", "clobTokenIds": '["t1"]'},
        source="test",
    )

    calls = []

    def fake_history(token_id, fidelity=60, **kw):
        calls.append(kw)
        return [{"t": 10, "p": 0.4}]

    monkeypatch.setattr(fetcher, "fetch_price_history", fake_history)

    archive.snapshot_all(conn, fidelity=60)
    assert calls[0].get("start_ts") is None            # first pass: full history

    stamp = conn.execute(
        "SELECT last_snapshot FROM tracked_markets").fetchone()[0]
    archive.snapshot_all(conn, fidelity=60)
    second = calls[1].get("start_ts")
    assert second is not None                          # second pass: delta only
    assert second <= stamp                             # overlap, never a gap


# ---------------------------------------------------------------------------
# run_once — cron entry point
# ---------------------------------------------------------------------------


def test_run_once_smoke(tmp_path, monkeypatch):
    from bot import fetcher
    from discovery import archive

    monkeypatch.setattr(
        fetcher, "fetch_top_markets",
        lambda closed, limit=50, end_date_min=None, end_date_max=None: (
            [] if closed else
            [{"conditionId": "0xa", "question": "A?", "clobTokenIds": '["ta1"]'}]
        ),
    )
    monkeypatch.setattr(fetcher, "fetch_global_trades", lambda *a, **kw: [])
    monkeypatch.setattr(
        fetcher, "fetch_price_history",
        lambda token_id, fidelity=60, **kw: [{"t": 1, "p": 0.9}],
    )

    summary = archive.run_once(db_path=str(tmp_path / "a.db"))
    assert summary["tracked_added"] == 1
    assert summary["points_added"] == 1
    assert summary["failures"] == 0
