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


def test_display_name_includes_username_when_set():
    """Notifications should self-identify which wallet they're mirroring."""
    a = _algo(CopyTradeParams(name="copy_trade", target_username="surfandturf"))
    assert a.display_name == "copy_trade → surfandturf"


def test_display_name_falls_back_to_short_address():
    """When only a wallet is configured, surface a shortened form."""
    a = _algo(CopyTradeParams(
        name="copy_trade",
        target_username="",
        target_address="0x1234567890abcdef1234567890abcdef12345678",
    ))
    assert a.display_name == "copy_trade → 0x1234…5678"


def test_display_name_is_bare_name_when_no_target():
    """No target configured (e.g. pre-setup) → no trailing arrow."""
    a = _algo(CopyTradeParams(name="copy_trade", target_username="", target_address=""))
    assert a.display_name == "copy_trade"


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


def test_smoke_test_wallet_mirrors_buy_one_to_one(algo_with_tracker):
    """When the target is the hard-coded smoke-test wallet, BUYs bypass the
    tier floor and mirror the target's USDC amount dollar-for-dollar."""
    from algorithms.copy_trade.algorithm import _SMOKE_TEST_WALLET
    algo_with_tracker._address = _SMOKE_TEST_WALLET
    t = make_trade(action="BUY", price=0.50, size_usdc=0.75)
    intents = list(algo_with_tracker._intents_for(t))
    assert len(intents) == 1
    assert isinstance(intents[0], OpenIntent)
    assert intents[0].usdc_amount == 0.75      # 1:1, not tier-scaled
    assert intents[0].reason == "smoke-test 1:1 mirror"


def test_smoke_test_wallet_mirrors_buy_case_insensitive(algo_with_tracker):
    """Address comparison is lowercased so checksum-cased env values still match."""
    from algorithms.copy_trade.algorithm import _SMOKE_TEST_WALLET
    algo_with_tracker._address = _SMOKE_TEST_WALLET.upper().replace("X", "x")
    t = make_trade(action="BUY", price=0.50, size_usdc=1.5)
    intents = list(algo_with_tracker._intents_for(t))
    assert len(intents) == 1
    assert intents[0].usdc_amount == 1.5


def test_smoke_test_wallet_full_close_on_sell(algo_with_tracker, tracker):
    """SELL from the smoke-test wallet → full close, no tier reasoning."""
    from algorithms.copy_trade.algorithm import _SMOKE_TEST_WALLET
    algo_with_tracker._address = _SMOKE_TEST_WALLET
    seed = make_trade(action="BUY", price=0.50)
    tracker.record_buy(seed, spent_usdc=0.50, shares=1.0, fill_price=0.50, paper=True)
    t = make_trade(action="SELL", price=0.55, size_usdc=0.27)
    intents = list(algo_with_tracker._intents_for(t))
    assert len(intents) == 1
    assert isinstance(intents[0], CloseIntent)
    assert intents[0].fraction == 1.0


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


# ── SELL: tier-resize semantics ─────────────────────────────────────────────
#
# The SELL handler resizes our position to match the target's POST-sell tier,
# symmetric to the BUY top-up. The fixture below sets up the canonical
# example: target had $400k (tier 3, our $3 position), then sells various
# amounts and we verify the resize is correct.


def _seed_position(tracker, cost: float, shares: float = 6.0, price: float = 0.5):
    """Helper: give the algorithm a position with known cost basis."""
    seed = make_trade(action="BUY", price=price)
    tracker.record_buy(seed, spent_usdc=cost, shares=shares,
                       fill_price=price, paper=True)


def test_sell_same_tier_no_op(algo_with_tracker, tracker):
    """Target sells $50k, still tier 3 ($350k) → our $3 stays unchanged."""
    _seed_position(tracker, cost=3.0)
    algo_with_tracker.holding_cache.set("m1", 400_000.0)

    t = make_trade(action="SELL", price=0.50, size_usdc=50_000.0)
    intents = list(algo_with_tracker._intents_for(t))
    assert intents == []
    # Cache decremented even on no-op so a subsequent sell re-tiers correctly.
    assert algo_with_tracker.holding_cache.get("m1") == 350_000.0


def test_sell_tier_3_to_tier_1_resize(algo_with_tracker, tracker):
    """The headline scenario: target had $400k ($3 ours), sells $300k →
    $100k left (tier 1) → we should hold $1 → close $2/$3 = 2/3 of position."""
    _seed_position(tracker, cost=3.0)
    algo_with_tracker.holding_cache.set("m1", 400_000.0)

    t = make_trade(action="SELL", price=0.50, size_usdc=300_000.0)
    intents = list(algo_with_tracker._intents_for(t))
    assert len(intents) == 1
    intent = intents[0]
    assert isinstance(intent, CloseIntent)
    # Sell 2/3 of position to leave $1 cost at the new tier 1.
    assert abs(intent.fraction - (2.0 / 3.0)) < 1e-9
    assert algo_with_tracker.holding_cache.get("m1") == 100_000.0


def test_sell_tier_3_to_below_min_full_close(algo_with_tracker, tracker):
    """Target sells $350k → $50k left, below tier1_min ($80k) → full close."""
    _seed_position(tracker, cost=3.0)
    algo_with_tracker.holding_cache.set("m1", 400_000.0)

    t = make_trade(action="SELL", price=0.50, size_usdc=350_000.0)
    intents = list(algo_with_tracker._intents_for(t))
    assert len(intents) == 1
    assert intents[0].fraction == 1.0


def test_sell_tier_1_to_below_min_full_close(algo_with_tracker, tracker):
    """Tier 1 ($150k) sells $80k → $70k, below min → full close, not 53%."""
    _seed_position(tracker, cost=1.0)
    algo_with_tracker.holding_cache.set("m1", 150_000.0)

    t = make_trade(action="SELL", price=0.50, size_usdc=80_000.0)
    intents = list(algo_with_tracker._intents_for(t))
    assert len(intents) == 1
    assert intents[0].fraction == 1.0


def test_sell_tier_2_to_tier_1(algo_with_tracker, tracker):
    """$200k tier 2 ($2 ours), sells $80k → $120k tier 1 ($1) → close half."""
    _seed_position(tracker, cost=2.0)
    algo_with_tracker.holding_cache.set("m1", 200_000.0)

    t = make_trade(action="SELL", price=0.50, size_usdc=80_000.0)
    intents = list(algo_with_tracker._intents_for(t))
    assert len(intents) == 1
    # Close $1 of $2 = 50%.
    assert abs(intents[0].fraction - 0.5) < 1e-9


def test_sell_cache_miss_falls_back_to_full_close(algo_with_tracker):
    """No cached pre-sell holding → safe default of fraction=1.0."""
    t = make_trade(action="SELL", price=0.50, size_usdc=25_000.0)
    intents = list(algo_with_tracker._intents_for(t))
    assert len(intents) == 1
    assert intents[0].fraction == 1.0
    assert intents[0].reason == "full close (cache miss)"


def test_sell_with_no_position_emits_no_intent(algo_with_tracker, tracker):
    """If the target sells but we never held the market, no SELL is emitted
    (the runner would no-op anyway, but skipping early saves a dispatch)."""
    algo_with_tracker.holding_cache.set("m1", 400_000.0)
    t = make_trade(action="SELL", price=0.50, size_usdc=300_000.0)
    intents = list(algo_with_tracker._intents_for(t))
    assert intents == []


def test_sell_oversize_caps_cache_at_zero(algo_with_tracker, tracker):
    """If the API-reported sell exceeds the cached holding (stale cache),
    the post-sell value floors at 0 → full close, not a negative cache."""
    _seed_position(tracker, cost=2.0)
    algo_with_tracker.holding_cache.set("m1", 50_000.0)   # stale, too small

    t = make_trade(action="SELL", price=0.50, size_usdc=100_000.0)
    intents = list(algo_with_tracker._intents_for(t))
    assert len(intents) == 1
    assert intents[0].fraction == 1.0
    assert algo_with_tracker.holding_cache.get("m1") == 0.0


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


# ---------------------------------------------------------------------------
# Settle sweep — periodic resolution check on our own open positions
# ---------------------------------------------------------------------------


@pytest.fixture
def algo_sweeping(tracker):
    """Algorithm wired for sweep testing: settle_check_every=1 so every poll triggers."""
    a = CopyTradeAlgorithm(params=CopyTradeParams(
        name="sweep_test",
        target_address="0xtarget",
        settle_check_every=1,
    ))
    a._tracker = tracker
    a._paper = True
    a._address = "0xtarget"
    return a


def _stub_poll(monkeypatch, trades=None):
    """Stub fetcher.fetch_recent_trades to return an empty list (no new signals)."""
    from bot import fetcher
    monkeypatch.setattr(fetcher, "fetch_recent_trades", lambda *a, **kw: trades or [])


def test_settle_sweep_emits_settle_for_resolved_market(
    algo_sweeping, tracker, monkeypatch
):
    from bot import fetcher

    seed = make_trade(action="BUY", market_id="m1", price=0.50)
    tracker.record_buy(seed, spent_usdc=1.0, shares=2.0, fill_price=0.50, paper=True)

    _stub_poll(monkeypatch)
    monkeypatch.setattr(
        fetcher, "fetch_market_resolution",
        lambda mid: {"closed": True, "outcomePrices": '["1", "0"]'},
    )

    intents = list(algo_sweeping.poll())
    assert len(intents) == 1
    assert isinstance(intents[0], SettleIntent)
    assert intents[0].market_id == "m1"
    assert intents[0].reason == "market resolved (sweep)"


def test_settle_sweep_skips_closed_but_undetermined_market(
    algo_sweeping, tracker, monkeypatch
):
    """Closed but outcome not yet binary (UMA dispute window) — must NOT settle."""
    from bot import fetcher

    seed = make_trade(action="BUY", market_id="m1", price=0.50)
    tracker.record_buy(seed, spent_usdc=1.0, shares=2.0, fill_price=0.50, paper=True)

    _stub_poll(monkeypatch)
    monkeypatch.setattr(
        fetcher, "fetch_market_resolution",
        lambda mid: {"closed": True, "outcomePrices": '["0.97", "0.03"]'},
    )
    assert list(algo_sweeping.poll()) == []


def test_settle_sweep_leaves_unresolved_markets_alone(
    algo_sweeping, tracker, monkeypatch
):
    from bot import fetcher

    seed = make_trade(action="BUY", market_id="m1", price=0.50)
    tracker.record_buy(seed, spent_usdc=1.0, shares=2.0, fill_price=0.50, paper=True)

    _stub_poll(monkeypatch)
    monkeypatch.setattr(
        fetcher, "fetch_market_resolution", lambda mid: {"closed": False},
    )
    assert list(algo_sweeping.poll()) == []


def test_settle_sweep_cadence(tracker, monkeypatch):
    """Sweep fires every `settle_check_every` polls, not every poll."""
    from bot import fetcher

    a = CopyTradeAlgorithm(params=CopyTradeParams(
        name="cadence_test",
        target_address="0xtarget",
        settle_check_every=3,
    ))
    a._tracker = tracker
    a._paper = True
    a._address = "0xtarget"

    seed = make_trade(action="BUY", market_id="m1", price=0.50)
    tracker.record_buy(seed, spent_usdc=1.0, shares=2.0, fill_price=0.50, paper=True)

    sweep_calls = []
    monkeypatch.setattr(fetcher, "fetch_recent_trades", lambda *a, **kw: [])
    monkeypatch.setattr(
        fetcher, "fetch_market_resolution",
        lambda mid: sweep_calls.append(mid) or {"closed": False},
    )

    list(a.poll())   # poll 1 — startup sweep fires immediately
    list(a.poll())   # poll 2 — no sweep
    list(a.poll())   # poll 3 — sweep fires (3 % 3 == 0)
    assert len(sweep_calls) == 2

    list(a.poll())   # poll 4 — no sweep
    list(a.poll())   # poll 5 — no sweep
    list(a.poll())   # poll 6 — sweep fires (6 % 3 == 0)
    assert len(sweep_calls) == 3
