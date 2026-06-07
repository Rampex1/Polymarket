"""
Fetcher tests.

The HTTP boundary (requests.Session) is stubbed — this is the only place
in the codebase where we have to stub because we obviously can't make
real Polymarket API calls in tests. Everything else (parsing, dedupe,
poll loop) runs the real code paths.
"""

import threading

import pytest


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
    from bot.models import Trade
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
