"""
Fetcher tests.

The HTTP boundary (requests.Session) is stubbed — this is the only place
in the codebase where we have to stub because we obviously can't make
real Polymarket API calls in tests. Everything else (parsing, dedupe,
poll loop) runs the real code paths.
"""

import threading



# ---------------------------------------------------------------------------
# Trade parsing — covers the shapes the activity API returns
# ---------------------------------------------------------------------------


def test_parse_trade_buy():
    from bot.fetcher import _parse_trade

    raw = {
        "type": "TRADE",
        "transactionHash": "0xabc",
        "conditionId": "m1",
        "title": "Will it rain?",
        "side": "BUY",
        "usdcSize": "100.5",
        "price": "0.43",
        "timestamp": "1700000000",
        "outcome": "Yes",
        "asset": "asset123",
    }
    t = _parse_trade(raw)
    assert t is not None
    assert t.action == "BUY"
    assert t.size_usdc == 100.5
    assert t.price == 0.43
    assert t.asset_id == "asset123"


def test_parse_trade_redeem_passes_through_without_asset():
    from bot.fetcher import _parse_trade

    t = _parse_trade({
        "type": "REDEEM",
        "transactionHash": "0xdef",
        "conditionId": "m1",
        "title": "...",
        "usdcSize": "50",
        "timestamp": "1700000000",
        "outcome": "Yes",
    })
    assert t is not None
    assert t.action == "REDEEM"


def test_parse_trade_merge_emits_signal():
    """MERGE rows must be emitted as a MERGE signal so the executor can
    mirror-close our matching position. Pre-fix, _parse_trade silently
    dropped them and our position would sit open indefinitely."""
    from bot.fetcher import _parse_trade

    t = _parse_trade({
        "type": "MERGE",
        "transactionHash": "0xmerge",
        "conditionId": "m1",
        "title": "Tennis match",
        "usdcSize": "100000",
        "timestamp": "1700000000",
        # No `asset` — a merge spans both outcomes.
    })
    assert t is not None
    assert t.action == "MERGE"
    assert t.market_id == "m1"
    assert t.size_usdc == 100_000.0
    assert t.asset_id is None


def test_parse_trade_merge_requires_tx_and_market():
    """A MERGE row missing tx_hash or conditionId can't be deduped or
    routed — drop it rather than emit a malformed signal."""
    from bot.fetcher import _parse_trade

    assert _parse_trade({"type": "MERGE", "conditionId": "m1"}) is None
    assert _parse_trade({"type": "MERGE", "transactionHash": "0x"}) is None


def test_parse_trade_emits_all_sizes_now_that_filter_moved():
    """The min-trade-size filter is now algorithm-level (see test_copy_trade.py).
    The fetcher emits every parseable row above $0 — small ones included."""
    from bot.fetcher import _parse_trade

    t = _parse_trade({
        "type": "TRADE", "side": "BUY", "usdcSize": "50",
        "price": "0.5", "transactionHash": "x", "conditionId": "m",
        "timestamp": "1", "outcome": "Yes", "asset": "a",
    })
    assert t is not None
    assert t.size_usdc == 50.0


def test_parse_trade_ignores_unknown_action():
    from bot.fetcher import _parse_trade

    assert _parse_trade({
        "type": "TRADE", "side": "TRANSFER", "usdcSize": "50",
        "price": "0.5", "transactionHash": "x", "conditionId": "m",
        "timestamp": "1", "outcome": "Yes", "asset": "a",
    }) is None


def test_parse_trade_malformed_returns_none():
    from bot.fetcher import _parse_trade

    # usdcSize=None is the realistic malformed shape; just ensure no crash.
    assert _parse_trade({"type": "TRADE", "side": "BUY"}) is None


# ---------------------------------------------------------------------------
# Bounded seen_ids LRU
# ---------------------------------------------------------------------------


def test_poll_loop_dedupes_each_trade_exactly_once(monkeypatch):
    """Re-fetching the same trades must not double-process."""
    from bot import fetcher

    next_id = [0]

    def fake_fetch(address, limit=100):
        new = next_id[0]
        next_id[0] += 1
        return [_fake_trade(f"id{i}") for i in range(max(0, new - 3), new + 1)]

    monkeypatch.setattr(fetcher, "fetch_recent_trades", fake_fetch)

    stop = threading.Event()
    seen_handled = []

    def on_trade(t):
        seen_handled.append(t.id)
        if len(seen_handled) >= 8:
            stop.set()

    fetcher.poll("0xtarget", on_trade=on_trade, stop_event=stop,
                 poll_interval_seconds=0)

    assert len(seen_handled) == len(set(seen_handled))
    assert len(seen_handled) >= 8


def test_seen_ids_lru_actually_evicts_oldest(monkeypatch):
    """Stronger than the dedupe test: forces the LRU to overflow with brand-
    new IDs every cycle and verifies that an *old* ID re-appearing AFTER the
    set has rolled past it triggers re-execution (proving the oldest entries
    are actually being evicted, not just held forever)."""
    from bot import fetcher

    # Tight cap so we can prove eviction in a few iterations.
    monkeypatch.setattr(fetcher, "SEEN_IDS_MAX", 3)

    # Seed phase: poll 1 returns id0; then we'll force-evict id0 by feeding
    # 3 distinct new IDs (so the ring is full of ids 1,2,3) and re-present id0.
    timeline = [
        [_fake_trade("id0")],         # seed at startup
        [_fake_trade("id1")],         # poll 1
        [_fake_trade("id2")],         # poll 2
        [_fake_trade("id3")],         # poll 3 — ring is now {id1,id2,id3}; id0 evicted
        [_fake_trade("id0")],         # poll 4 — should re-execute id0
    ]

    def fake_fetch(address, limit=100):
        return timeline.pop(0) if timeline else []

    monkeypatch.setattr(fetcher, "fetch_recent_trades", fake_fetch)

    stop = threading.Event()
    handled = []

    def on_trade(t):
        handled.append(t.id)
        if not timeline:
            stop.set()

    fetcher.poll("0xtarget", on_trade=on_trade, stop_event=stop,
                 poll_interval_seconds=0)

    # id0 was seeded (not handled), then evicted, then handled on re-appearance.
    assert "id0" in handled, "id0 should have been re-executed after eviction"


def _fake_trade(tx_id):
    from bot.domain.records import Trade
    return Trade(
        id=tx_id, market_id="m", question="q", side="BUY", size_usdc=1.0,
        price=0.5, action="BUY", timestamp=int(tx_id[2:]) if tx_id[2:].isdigit() else 0,
        outcome="Yes", asset_id="a",
    )


# ---------------------------------------------------------------------------
# Poll loop tolerates handler exceptions
# ---------------------------------------------------------------------------


def test_poll_loop_swallows_handler_exceptions(monkeypatch):
    """One bad on_trade call must not kill the loop."""
    from bot import fetcher

    counter = {"n": 0}

    def fake_fetch(address, limit=100):
        counter["n"] += 1
        # Always returns ONE new trade per cycle.
        return [_fake_trade(f"id{counter['n']}")]

    monkeypatch.setattr(fetcher, "fetch_recent_trades", fake_fetch)

    stop = threading.Event()
    invocations = []

    def on_trade(t):
        invocations.append(t.id)
        if len(invocations) == 1:
            raise RuntimeError("boom")
        if len(invocations) >= 3:
            stop.set()

    # Should not raise.
    fetcher.poll("0xtarget", on_trade=on_trade, stop_event=stop,
                 poll_interval_seconds=0)
    assert len(invocations) >= 3


# ---------------------------------------------------------------------------
# fetch_target_position_value — retry-until-consistent semantics
# ---------------------------------------------------------------------------


def test_fetch_target_position_retries_until_expected_min(monkeypatch):
    """When expected_min is set, the function should re-query if the API
    hasn't caught up yet (eventual consistency)."""
    from bot import fetcher

    state = {"calls": 0}

    class FakeResp:
        status_code = 200

        def __init__(self, val):
            self.val = val

        def raise_for_status(self):
            pass

        def json(self):
            return [{"value": self.val}]

    def fake_get(url, params=None, timeout=None):
        state["calls"] += 1
        # First two calls return stale 0, third returns the correct value.
        return FakeResp(0.0 if state["calls"] < 3 else 100_000.0)

    monkeypatch.setattr(fetcher.SESSION, "get", fake_get)
    # Skip the inter-attempt sleep to keep the test fast.
    monkeypatch.setattr(fetcher.time, "sleep", lambda _s: None)

    val = fetcher.fetch_target_position_value(
        "0xtarget", "m1", expected_min=50_000.0, retries=5, retry_wait=0.0,
    )
    assert val == 100_000.0
    assert state["calls"] == 3


def test_fetch_target_position_returns_last_value_after_retries(monkeypatch):
    """If the API never catches up, return the most recent value (don't loop forever)."""
    from bot import fetcher

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return [{"value": 0.0}]

    monkeypatch.setattr(fetcher.SESSION, "get", lambda *a, **kw: FakeResp())
    monkeypatch.setattr(fetcher.time, "sleep", lambda _s: None)

    val = fetcher.fetch_target_position_value(
        "0xtarget", "m1", expected_min=100_000.0, retries=2, retry_wait=0.0,
    )
    assert val == 0.0


def test_fetch_recent_trades_handles_api_failure(monkeypatch):
    """Network errors must not crash — they return an empty list."""
    from bot import fetcher
    import requests

    def bad_get(*a, **kw):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(fetcher.SESSION, "get", bad_get)
    assert fetcher.fetch_recent_trades("0xtarget") == []


# ---------------------------------------------------------------------------
# Global trades firehose (data-api /trades, platform-wide)
# ---------------------------------------------------------------------------


def _firehose_row(**overrides):
    """A realistic /trades row (verified shape, June 2026)."""
    row = {
        "proxyWallet": "0xwhale",
        "side": "BUY",
        "asset": "tok1",
        "conditionId": "0xcond",
        "size": 1000.0,            # shares, NOT usd
        "price": 0.25,
        "timestamp": 1_700_000_000,
        "title": "Will X resign?",
        "outcome": "Yes",
        "name": "anon123",
        "pseudonym": "Quiet-Fox",
        "transactionHash": "0xtx",
    }
    row.update(overrides)
    return row


def test_parse_global_trade_maps_fields_and_computes_cash():
    from bot.fetcher import _parse_global_trade

    t = _parse_global_trade(_firehose_row())
    assert t is not None
    assert t.wallet == "0xwhale"
    assert t.side == "BUY"
    assert t.price == 0.25
    assert t.shares == 1000.0
    assert t.cash_usdc == 250.0          # shares × price, computed at parse
    assert t.market_id == "0xcond"
    assert t.asset_id == "tok1"
    assert t.tx_hash == "0xtx"
    assert t.timestamp == 1_700_000_000
    assert t.title == "Will X resign?"


def test_parse_global_trade_rejects_missing_required_fields():
    from bot.fetcher import _parse_global_trade

    for missing in ("transactionHash", "conditionId", "proxyWallet", "asset"):
        assert _parse_global_trade(_firehose_row(**{missing: None})) is None
    # Non-positive economics are unusable for sizing/odds decisions.
    assert _parse_global_trade(_firehose_row(price=0)) is None
    assert _parse_global_trade(_firehose_row(size=0)) is None
    # SELL rows are kept — the *algorithm* decides what to do with sides.
    assert _parse_global_trade(_firehose_row(side="SELL")) is not None


def test_fetch_global_trades_builds_cash_filter_params(monkeypatch):
    from bot import fetcher

    captured = {}

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return [_firehose_row()]

    def fake_get(url, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        return FakeResp()

    monkeypatch.setattr(fetcher.SESSION, "get", fake_get)
    out = fetcher.fetch_global_trades(min_cash_usdc=5000, limit=50)

    assert captured["url"].endswith("/trades")
    assert captured["params"]["filterType"] == "CASH"
    assert captured["params"]["filterAmount"] == 5000
    assert captured["params"]["limit"] == 50
    assert len(out) == 1
    assert out[0].wallet == "0xwhale"


def test_fetch_global_trades_handles_api_failure(monkeypatch):
    from bot import fetcher
    import requests

    def bad_get(*a, **kw):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(fetcher.SESSION, "get", bad_get)
    assert fetcher.fetch_global_trades(min_cash_usdc=5000) == []


# ---------------------------------------------------------------------------
# Wallet stats — freshness primitive for insider-flow detection
# ---------------------------------------------------------------------------


class _StatsResp:
    status_code = 200

    def __init__(self, rows):
        self.rows = rows

    def raise_for_status(self):
        pass

    def json(self):
        return self.rows


def test_fetch_wallet_stats_counts_trades_and_oldest(monkeypatch):
    from bot import fetcher

    rows = [
        {"type": "TRADE", "timestamp": 1_700_000_300},
        {"type": "REDEEM", "timestamp": 1_700_000_200},
        {"type": "TRADE", "timestamp": 1_700_000_100},
    ]
    monkeypatch.setattr(fetcher.SESSION, "get", lambda *a, **kw: _StatsResp(rows))

    stats = fetcher.fetch_wallet_stats("0xw", max_rows=100)
    assert stats == {
        "trade_count": 2,
        "activity_count": 3,
        "oldest_ts": 1_700_000_100,
        "capped": False,
    }


def test_fetch_wallet_stats_full_page_is_capped(monkeypatch):
    """A full page means the wallet has ≥ max_rows activities — definitely
    not a fresh wallet; callers short-circuit on `capped`."""
    from bot import fetcher

    rows = [{"type": "TRADE", "timestamp": 1_700_000_000 + i} for i in range(5)]
    monkeypatch.setattr(fetcher.SESSION, "get", lambda *a, **kw: _StatsResp(rows))

    stats = fetcher.fetch_wallet_stats("0xw", max_rows=5)
    assert stats["capped"] is True


def test_fetch_wallet_stats_empty_history_is_fresh(monkeypatch):
    """Zero activities (API lag on a brand-new wallet) → fresh, not an error."""
    from bot import fetcher

    monkeypatch.setattr(fetcher.SESSION, "get", lambda *a, **kw: _StatsResp([]))
    stats = fetcher.fetch_wallet_stats("0xw")
    assert stats == {
        "trade_count": 0, "activity_count": 0, "oldest_ts": None, "capped": False,
    }


def test_fetch_wallet_stats_ignores_null_timestamps(monkeypatch):
    """A row whose timestamp hasn't propagated must not register as
    epoch-zero — that would make a brand-new wallet look decades old and
    silently fail every freshness check."""
    from bot import fetcher

    rows = [
        {"type": "TRADE", "timestamp": None},
        {"type": "TRADE", "timestamp": 1_700_000_100},
    ]
    monkeypatch.setattr(fetcher.SESSION, "get", lambda *a, **kw: _StatsResp(rows))
    assert fetcher.fetch_wallet_stats("0xw")["oldest_ts"] == 1_700_000_100

    rows_all_null = [{"type": "TRADE", "timestamp": None}]
    monkeypatch.setattr(
        fetcher.SESSION, "get", lambda *a, **kw: _StatsResp(rows_all_null))
    assert fetcher.fetch_wallet_stats("0xw")["oldest_ts"] is None


def test_fetch_wallet_stats_failure_returns_none(monkeypatch):
    """None (not a dict) so callers can fail CLOSED on unverifiable wallets."""
    from bot import fetcher
    import requests

    def bad_get(*a, **kw):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(fetcher.SESSION, "get", bad_get)
    assert fetcher.fetch_wallet_stats("0xw") is None


def test_fetch_wallet_value_parses_list_shape(monkeypatch):
    from bot import fetcher

    monkeypatch.setattr(
        fetcher.SESSION, "get",
        lambda *a, **kw: _StatsResp([{"user": "0xw", "value": 2500.5}]),
    )
    assert fetcher.fetch_wallet_value("0xw") == 2500.5


def test_fetch_wallet_value_parses_dict_shape(monkeypatch):
    from bot import fetcher

    monkeypatch.setattr(
        fetcher.SESSION, "get", lambda *a, **kw: _StatsResp({"value": "99.5"}),
    )
    assert fetcher.fetch_wallet_value("0xw") == 99.5


def test_fetch_wallet_value_failure_returns_none(monkeypatch):
    from bot import fetcher
    import requests

    def bad_get(*a, **kw):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(fetcher.SESSION, "get", bad_get)
    assert fetcher.fetch_wallet_value("0xw") is None


def test_fetch_wallet_value_unparseable_returns_none(monkeypatch):
    from bot import fetcher

    monkeypatch.setattr(
        fetcher.SESSION, "get", lambda *a, **kw: _StatsResp([{"user": "0xw"}]),
    )
    assert fetcher.fetch_wallet_value("0xw") is None


# ---------------------------------------------------------------------------
# Price history (CLOB /prices-history) — archiver + timing backbone
# ---------------------------------------------------------------------------


def test_fetch_price_history_parses_points(monkeypatch):
    from bot import fetcher

    captured = {}

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"history": [{"t": 1, "p": 0.5}, {"t": 2, "p": 0.6}]}

    def fake_get(url, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        return FakeResp()

    monkeypatch.setattr(fetcher.SESSION, "get", fake_get)
    pts = fetcher.fetch_price_history("tok1", fidelity=60)

    assert captured["url"].endswith("/prices-history")
    assert captured["params"]["market"] == "tok1"
    assert captured["params"]["fidelity"] == 60
    assert captured["params"]["interval"] == "max"
    assert pts == [{"t": 1, "p": 0.5}, {"t": 2, "p": 0.6}]


def test_fetch_price_history_ts_range_replaces_interval(monkeypatch):
    """startTs/endTs and interval are mutually exclusive on the CLOB API."""
    from bot import fetcher

    captured = {}

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"history": []}

    def fake_get(url, params=None, timeout=None):
        captured["params"] = params
        return FakeResp()

    monkeypatch.setattr(fetcher.SESSION, "get", fake_get)
    pts = fetcher.fetch_price_history("tok1", fidelity=1, start_ts=100, end_ts=200)

    assert captured["params"]["startTs"] == 100
    assert captured["params"]["endTs"] == 200
    assert "interval" not in captured["params"]
    assert pts == []          # empty history is a real answer (old market)…


def test_fetch_price_history_failure_returns_none(monkeypatch):
    """…whereas None means the HTTP call itself failed (caller may retry)."""
    from bot import fetcher
    import requests

    def bad_get(*a, **kw):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(fetcher.SESSION, "get", bad_get)
    assert fetcher.fetch_price_history("tok1") is None


# ---------------------------------------------------------------------------
# Top markets (Gamma) — universe selection
# ---------------------------------------------------------------------------


def test_fetch_top_markets_builds_params(monkeypatch):
    from bot import fetcher

    captured = {}

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return [{"conditionId": "0xc"}]

    def fake_get(url, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        return FakeResp()

    monkeypatch.setattr(fetcher.SESSION, "get", fake_get)
    out = fetcher.fetch_top_markets(closed=True, limit=25, end_date_min="2026-06-01")

    assert captured["url"].endswith("/markets")
    # Gamma is case-sensitive about booleans — must be lowercase strings.
    assert captured["params"]["closed"] == "true"
    assert captured["params"]["order"] == "volumeNum"
    assert captured["params"]["ascending"] == "false"
    assert captured["params"]["limit"] == 25
    assert captured["params"]["end_date_min"] == "2026-06-01"
    assert out == [{"conditionId": "0xc"}]


def test_fetch_top_markets_failure_returns_empty(monkeypatch):
    from bot import fetcher
    import requests

    def bad_get(*a, **kw):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(fetcher.SESSION, "get", bad_get)
    assert fetcher.fetch_top_markets(closed=False) == []
