"""
CopyTrade algorithm tests — tier mapping, intent emission, MERGE/SELL/REDEEM
classification. The fetcher HTTP boundary is stubbed; everything else
runs the real code paths.
"""

import pytest
from dataclasses import replace

from tests.conftest import make_trade

from algorithms.copy_trade.algorithm import CopyTradeAlgorithm
from algorithms.copy_trade.params import CopyTradeParams
from bot.algorithm import CloseIntent, OpenIntent, SettleIntent


# ---------------------------------------------------------------------------
# Tier mapping — pure function on the algorithm
# ---------------------------------------------------------------------------


def _algo(params=None):
    algo = CopyTradeAlgorithm()
    if params is not None:
        algo.params = params
    return algo


def test_tier_boundaries():
    a = _algo(CopyTradeParams())
    assert a._tier_for_holding(80_000) == 1.0
    assert a._tier_for_holding(150_000) == 1.0           # inclusive
    assert a._tier_for_holding(150_001) == 2.0
    assert a._tier_for_holding(300_000) == 2.0           # inclusive
    assert a._tier_for_holding(300_001) == 3.0
    assert a._tier_for_holding(1_000_000) == 3.0


# ---------------------------------------------------------------------------
# Intent emission per signal type
# ---------------------------------------------------------------------------


@pytest.fixture
def algo_with_tracker(tracker, default_params, monkeypatch):
    """A CopyTradeAlgorithm wired to the test tracker, no live network."""
    a = _algo(default_params)
    a._tracker = tracker
    a._paper = True
    a._address = "0xtarget"
    return a


@pytest.fixture
def stub_holding(monkeypatch):
    """Pin the target-holding lookup."""
    from bot import fetcher

    def _set(value):
        monkeypatch.setattr(
            fetcher, "fetch_target_position_value", lambda *a, **kw: value,
        )
    return _set


def test_buy_signal_yields_open_intent_when_above_tier_min(
    algo_with_tracker, stub_holding
):
    stub_holding(100_000)
    t = make_trade(action="BUY", price=0.50)
    intents = list(algo_with_tracker._intents_for(t))
    assert len(intents) == 1
    intent = intents[0]
    assert isinstance(intent, OpenIntent)
    assert intent.usdc_amount == 1.0      # tier 1
    assert intent.signal_price == 0.50


def test_buy_signal_skipped_below_tier1_min(algo_with_tracker, stub_holding):
    stub_holding(50_000)
    t = make_trade(action="BUY", price=0.50)
    assert list(algo_with_tracker._intents_for(t)) == []


def test_buy_signal_tops_up_to_target(algo_with_tracker, stub_holding, tracker,
                                       default_params, monkeypatch):
    """When we already hold $0.50 in the market and tier is $1, top-up = $0.50.
    The min-order gate normally vetoes that — relax it so we can verify the math."""
    monkeypatch.setattr(default_params, "min_order_size_usdc", 0.10)

    seed = make_trade(action="BUY", price=0.50)
    tracker.record_buy(seed, spent_usdc=0.50, shares=1.0, fill_price=0.50, paper=True)

    stub_holding(100_000)
    t = make_trade(action="BUY", price=0.50, trade_id="tx2")
    intents = list(algo_with_tracker._intents_for(t))
    assert len(intents) == 1
    assert abs(intents[0].usdc_amount - 0.50) < 1e-9


def test_buy_signal_skipped_when_at_tier(algo_with_tracker, stub_holding, tracker):
    """Position cost already equals tier target → no top-up."""
    seed = make_trade(action="BUY", price=0.50)
    tracker.record_buy(seed, spent_usdc=1.0, shares=2.0, fill_price=0.50, paper=True)
    stub_holding(100_000)
    t = make_trade(action="BUY", price=0.50, trade_id="tx2")
    assert list(algo_with_tracker._intents_for(t)) == []


def test_sell_signal_yields_proportional_close(algo_with_tracker):
    """The cached pre-sell holding sets the close ratio."""
    algo_with_tracker.holding_cache.set("m1", 100_000.0)
    t = make_trade(action="SELL", price=0.50, size_usdc=25_000.0)
    intents = list(algo_with_tracker._intents_for(t))
    assert len(intents) == 1
    assert isinstance(intents[0], CloseIntent)
    assert abs(intents[0].fraction - 0.25) < 1e-9
    # Cache decremented for subsequent partials.
    assert abs(algo_with_tracker.holding_cache.get("m1") - 75_000.0) < 1e-6


def test_sell_signal_full_close_on_cache_miss(algo_with_tracker):
    """No cached pre-sell holding → safe default of fraction=1.0."""
    t = make_trade(action="SELL", price=0.50, size_usdc=25_000.0)
    intents = list(algo_with_tracker._intents_for(t))
    assert len(intents) == 1
    assert isinstance(intents[0], CloseIntent)
    assert intents[0].fraction == 1.0


def test_merge_signal_yields_full_close(algo_with_tracker):
    """MERGE → CloseIntent with fraction=1.0 and signal_price=0 (no slip gate)."""
    algo_with_tracker.holding_cache.set("m1", 100_000.0)
    t = make_trade(action="MERGE", price=0.0, size_usdc=100_000.0)
    intents = list(algo_with_tracker._intents_for(t))
    assert len(intents) == 1
    intent = intents[0]
    assert isinstance(intent, CloseIntent)
    assert intent.fraction == 1.0
    assert intent.signal_price == 0.0
    # Cache cleared so subsequent signals don't compute off stale value.
    assert algo_with_tracker.holding_cache.get("m1") == 0.0


def test_redeem_signal_yields_settle_intent(algo_with_tracker):
    t = make_trade(action="REDEEM", price=0.0)
    intents = list(algo_with_tracker._intents_for(t))
    assert len(intents) == 1
    assert isinstance(intents[0], SettleIntent)


# ---------------------------------------------------------------------------
# Dedupe + min-trade-size filter in poll()
# ---------------------------------------------------------------------------


def test_poll_dedupes_seen_ids(algo_with_tracker, monkeypatch, stub_holding):
    from bot import fetcher

    state = {"calls": 0}

    def fake_fetch(addr, limit=100):
        state["calls"] += 1
        return [make_trade(action="BUY", trade_id="tx1", price=0.50)]

    monkeypatch.setattr(fetcher, "fetch_recent_trades", fake_fetch)
    stub_holding(100_000)

    # First poll yields an intent.
    intents1 = list(algo_with_tracker.poll())
    assert len(intents1) == 1
    # Second poll: same trade ID, already in seen_ids → no intent.
    intents2 = list(algo_with_tracker.poll())
    assert intents2 == []


def test_poll_filters_below_min_trade_size(algo_with_tracker, monkeypatch,
                                            default_params, stub_holding):
    """Dust trades below `min_trade_size_usdc` get dropped before classification."""
    from bot import fetcher

    monkeypatch.setattr(default_params, "min_trade_size_usdc", 100.0)
    monkeypatch.setattr(
        fetcher, "fetch_recent_trades",
        lambda addr, limit=100: [
            make_trade(action="BUY", trade_id="dust", size_usdc=10.0, price=0.50),
        ],
    )
    stub_holding(100_000)
    assert list(algo_with_tracker.poll()) == []
