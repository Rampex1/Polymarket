"""
Executor tests — tier mapping, slippage, simulated fills, fill parsing,
and full integration flows through `execute()`.

The HTTP boundary (target-holding lookup, current price, order placement)
is stubbed. Everything else — risk checks, position recording, DB writes,
P&L computation — runs through the real code paths.
"""

import pytest

from tests.conftest import make_trade


# ---------------------------------------------------------------------------
# _tier_for_holding — pure function
# ---------------------------------------------------------------------------


def test_tier_boundaries(default_config):
    from bot.executor import _tier_for_holding
    # At/below tier1 max → tier1
    assert _tier_for_holding(80_000) == 1.0
    assert _tier_for_holding(150_000) == 1.0       # boundary inclusive
    # Between tier1 max and tier2 max → tier2
    assert _tier_for_holding(150_001) == 2.0
    assert _tier_for_holding(300_000) == 2.0       # boundary inclusive
    # Above tier2 max → tier3
    assert _tier_for_holding(300_001) == 3.0
    assert _tier_for_holding(1_000_000) == 3.0


# ---------------------------------------------------------------------------
# _slippage_ok
# ---------------------------------------------------------------------------


def test_slippage_ok_within_tolerance(default_config):
    from bot.executor import _slippage_ok
    t = make_trade(price=0.50)
    assert _slippage_ok(t, current_price=0.52)         # 4% drift
    # Just under the 5% threshold (floating-point safe).
    assert _slippage_ok(t, current_price=0.4751)


def test_slippage_ok_rejects_excess_drift(default_config):
    from bot.executor import _slippage_ok
    t = make_trade(price=0.50)
    assert not _slippage_ok(t, current_price=0.60)     # 20% drift


def test_slippage_ok_with_zero_signal_price(default_config):
    """REDEEM signals have price=0 — must not div-by-zero."""
    from bot.executor import _slippage_ok
    t = make_trade(price=0.0)
    assert _slippage_ok(t, current_price=0.5)


# ---------------------------------------------------------------------------
# Simulated fills — paper-mode realism
# ---------------------------------------------------------------------------


def test_simulate_buy_uses_current_price(default_config):
    """Paper fill must use the *current* price, not the target's signal price.
    This was a bug pre-fix: paper P&L was inflated by always filling at
    the target's price."""
    from bot.executor import _simulate_buy
    t = make_trade(price=0.40)        # target's signal price
    fill = _simulate_buy(t, scaled_usdc=10.0, current_price=0.50)
    assert fill.success
    assert fill.fill_price == 0.50
    assert fill.shares == 20.0
    assert fill.spent_usdc == 10.0


def test_simulate_buy_charges_fee(default_config, monkeypatch):
    from bot.executor import _simulate_buy
    monkeypatch.setattr(default_config, "PAPER_FEE_BPS", 200.0)   # 2%
    t = make_trade()
    fill = _simulate_buy(t, scaled_usdc=100.0, current_price=0.50)
    assert fill.fee_usdc == 2.0
    assert fill.shares == 98.0 / 0.50          # 196 shares


def test_simulate_buy_rejects_zero_price(default_config):
    from bot.executor import _simulate_buy
    t = make_trade(price=0.0)
    fill = _simulate_buy(t, scaled_usdc=10.0, current_price=0.0)
    assert not fill.success


def test_simulate_sell_proceeds_and_fee(default_config, monkeypatch):
    from bot.executor import _simulate_sell
    monkeypatch.setattr(default_config, "PAPER_FEE_BPS", 200.0)
    t = make_trade(action="SELL")
    fill = _simulate_sell(t, shares=100.0, current_price=0.70)
    assert fill.spent_usdc == 70.0
    assert abs(fill.fee_usdc - 1.40) < 1e-9


# ---------------------------------------------------------------------------
# _parse_fill — order-response parsing
# ---------------------------------------------------------------------------


def test_parse_fill_rejected_response_returns_failure():
    from bot.executor import _parse_fill
    fill = _parse_fill({"success": False, "errorMsg": "no liquidity"},
                       requested_usdc=10.0, signal_price=0.5, side="BUY")
    assert not fill.success


def test_parse_fill_unmatched_status_returns_failure():
    from bot.executor import _parse_fill
    fill = _parse_fill({"status": "unmatched"}, 10.0, 0.5, "BUY")
    assert not fill.success


def test_parse_fill_uses_actual_amounts_for_buy():
    """Pre-fix bug: cost basis was derived from the target's signal price,
    not from the actual fill. We assert we use the response amounts."""
    from bot.executor import _parse_fill
    fill = _parse_fill(
        {"success": True, "takingAmount": 10.0, "makingAmount": 18.0},
        requested_usdc=10.0, signal_price=0.5, side="BUY",
    )
    assert fill.success
    assert fill.spent_usdc == 10.0
    assert fill.shares == 18.0
    # fill price implied by actual fill, not signal_price
    assert abs(fill.fill_price - (10.0 / 18.0)) < 1e-9


def test_parse_fill_uses_actual_amounts_for_sell():
    from bot.executor import _parse_fill
    fill = _parse_fill(
        {"success": True, "takingAmount": 20.0, "makingAmount": 14.0},
        requested_usdc=10.0, signal_price=0.5, side="SELL",
    )
    assert fill.success
    # SELL: makingAmount is proceeds, takingAmount is shares
    assert fill.spent_usdc == 14.0
    assert fill.shares == 20.0


def test_parse_fill_falls_back_to_request_when_amounts_missing():
    """Some success responses don't include taking/makingAmount; fall back
    to the requested values rather than crashing or recording zero."""
    from bot.executor import _parse_fill
    fill = _parse_fill({"success": True}, requested_usdc=10.0,
                       signal_price=0.5, side="BUY")
    assert fill.success
    assert fill.spent_usdc == 10.0
    assert fill.shares == 20.0


# ---------------------------------------------------------------------------
# Full execute() integration — paper mode
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_holding(monkeypatch):
    """Pin the target-holding lookup to a deterministic value."""
    from bot import fetcher

    def _set(value):
        monkeypatch.setattr(
            fetcher,
            "fetch_target_position_value",
            lambda *a, **kw: value,
        )
    return _set


@pytest.fixture
def stub_price(monkeypatch):
    """Pin _get_current_price for slippage + fill simulation."""
    from bot import executor

    def _set(value):
        monkeypatch.setattr(
            executor,
            "_get_current_price",
            lambda *a, **kw: value,
        )
    return _set


def test_execute_buy_tier1_paper(tracker, risk, default_config, stub_holding, stub_price):
    """End-to-end BUY in paper: holding in tier1 → $1 bet → position recorded."""
    from bot import executor

    stub_holding(100_000)          # tier1
    stub_price(0.50)               # current market = signal price → no slippage

    t = make_trade(action="BUY", price=0.50)
    executor.execute(t, client=None, tracker=tracker, risk=risk)

    p = tracker.get("m1", paper=True)
    assert p is not None
    assert p.total_cost_usdc == 1.0          # tier1 size
    # Paper balance decreased by $1.
    assert tracker.paper_balance() == 10_000.0 - 1.0


def test_execute_buy_skipped_below_tier1(tracker, risk, default_config, stub_holding, stub_price):
    """Holding below TIER1_MIN → no order at all."""
    from bot import executor

    stub_holding(50_000)
    stub_price(0.50)
    t = make_trade(action="BUY")
    executor.execute(t, client=None, tracker=tracker, risk=risk)
    assert tracker.get("m1", paper=True) is None
    assert tracker.paper_balance() == 10_000.0


def test_execute_buy_skipped_by_slippage(tracker, risk, default_config, stub_holding, stub_price):
    """If market drifted beyond MAX_SLIPPAGE since signal, no position recorded."""
    from bot import executor

    stub_holding(100_000)
    stub_price(0.70)                # vs signal 0.50 → 40% drift > 5%

    t = make_trade(action="BUY", price=0.50)
    executor.execute(t, client=None, tracker=tracker, risk=risk)
    assert tracker.get("m1", paper=True) is None


def test_execute_buy_skipped_by_min_order(tracker, risk, default_config,
                                          stub_holding, stub_price, monkeypatch):
    """A tier bet below MIN_ORDER_SIZE_USDC must be rejected by risk gate."""
    from bot import executor

    monkeypatch.setattr(default_config, "TIER1_SIZE", 0.5)        # below $1 min
    monkeypatch.setattr(default_config, "MIN_ORDER_SIZE_USDC", 1.0)

    stub_holding(100_000)
    stub_price(0.50)
    t = make_trade(action="BUY")
    executor.execute(t, client=None, tracker=tracker, risk=risk)
    assert tracker.get("m1", paper=True) is None


def test_execute_sell_proportional_close(tracker, risk, default_config,
                                         stub_holding, stub_price, monkeypatch):
    """SELL closes the mirror in proportion to target's sell ratio."""
    from bot import executor, fetcher

    # Seed an open paper position: 20 shares @ $0.50 = $10 cost.
    seed = make_trade(action="BUY", price=0.50)
    tracker.record_buy(seed, spent_usdc=10.0, shares=20.0, fill_price=0.50, paper=True)

    stub_price(0.50)

    # Target had $100k position, sells $25k → 25% sell ratio.
    # We stub fetch_target_position_value to return $75k (after the sell).
    monkeypatch.setattr(fetcher, "fetch_target_position_value",
                        lambda *a, **kw: 75_000.0)
    sell = make_trade(action="SELL", price=0.50, size_usdc=25_000.0, trade_id="s1")

    executor.execute(sell, client=None, tracker=tracker, risk=risk)

    p = tracker.get("m1", paper=True)
    # Should have closed 25% of 20 shares = 5 shares; 15 remain.
    assert p is not None
    assert abs(p.shares - 15.0) < 1e-6
    assert p.avg_price == 0.50


def test_execute_sell_with_no_position_is_noop(tracker, risk, default_config,
                                               stub_holding, stub_price):
    from bot import executor
    stub_price(0.50)
    sell = make_trade(action="SELL", price=0.50)
    executor.execute(sell, client=None, tracker=tracker, risk=risk)
    # No exception, no position created.
    assert tracker.get("m1", paper=True) is None


def test_execute_no_asset_id_buy_is_skipped(tracker, risk, default_config):
    from bot import executor
    t = make_trade(action="BUY", asset_id=None)
    executor.execute(t, client=None, tracker=tracker, risk=risk)
    assert tracker.get("m1", paper=True) is None


# ---------------------------------------------------------------------------
# REDEEM resolution
# ---------------------------------------------------------------------------


def test_redeem_settles_at_one_for_winning_token(tracker, risk, default_config, monkeypatch):
    """A redeem with last-trade ~1.0 closes the position at 1.0 (WIN)."""
    from bot import executor, fetcher

    seed = make_trade(action="BUY", price=0.40)
    tracker.record_buy(seed, spent_usdc=4.0, shares=10.0, fill_price=0.40, paper=True)

    # No Gamma data, fall back to CLOB last-trade-price.
    monkeypatch.setattr(fetcher, "fetch_market_resolution", lambda *a, **kw: None)
    monkeypatch.setattr(fetcher, "fetch_resolution_price", lambda *a, **kw: 0.99)

    redeem = make_trade(action="REDEEM", price=0.0, trade_id="r1")
    executor.execute(redeem, client=None, tracker=tracker, risk=risk)

    # Position closed; P&L = (1.0 - 0.40) * 10 = 6.0
    assert tracker.get("m1", paper=True) is None
    assert tracker.today_pnl_usdc(paper=True) == 6.0


def test_redeem_settles_at_zero_for_losing_token(tracker, risk, default_config, monkeypatch):
    from bot import executor, fetcher

    seed = make_trade(action="BUY", price=0.40)
    tracker.record_buy(seed, spent_usdc=4.0, shares=10.0, fill_price=0.40, paper=True)

    monkeypatch.setattr(fetcher, "fetch_market_resolution", lambda *a, **kw: None)
    monkeypatch.setattr(fetcher, "fetch_resolution_price", lambda *a, **kw: 0.01)

    redeem = make_trade(action="REDEEM", price=0.0, trade_id="r1")
    executor.execute(redeem, client=None, tracker=tracker, risk=risk)

    assert tracker.get("m1", paper=True) is None
    # P&L = (0.0 - 0.40) * 10 = -4.0
    assert tracker.today_pnl_usdc(paper=True) == -4.0


def test_redeem_with_ambiguous_price_leaves_position_open(tracker, risk, default_config, monkeypatch):
    """Pre-fix: an ambiguous CLOB price (0.5) used to silently leave positions
    open — but the new Gamma fallback would resolve. With both unavailable,
    we still leave it open rather than guess."""
    from bot import executor, fetcher

    seed = make_trade(action="BUY", price=0.40)
    tracker.record_buy(seed, spent_usdc=4.0, shares=10.0, fill_price=0.40, paper=True)

    monkeypatch.setattr(fetcher, "fetch_market_resolution", lambda *a, **kw: None)
    monkeypatch.setattr(fetcher, "fetch_resolution_price", lambda *a, **kw: 0.50)

    redeem = make_trade(action="REDEEM", price=0.0, trade_id="r1")
    executor.execute(redeem, client=None, tracker=tracker, risk=risk)

    # Position should remain — better safe than wrong.
    assert tracker.get("m1", paper=True) is not None


def test_redeem_uses_gamma_resolution_when_available(tracker, risk, default_config, monkeypatch):
    """If Gamma exposes outcome prices, use them as the canonical resolution."""
    from bot import executor, fetcher

    seed = make_trade(action="BUY", price=0.40, asset_id="winner")
    tracker.record_buy(seed, spent_usdc=4.0, shares=10.0, fill_price=0.40, paper=True)

    monkeypatch.setattr(
        fetcher,
        "fetch_market_resolution",
        lambda *a, **kw: {
            "clobTokenIds": '["winner", "loser"]',
            "outcomePrices": '["1.0", "0.0"]',
        },
    )
    # Make sure CLOB fallback isn't relied on.
    monkeypatch.setattr(fetcher, "fetch_resolution_price",
                        lambda *a, **kw: pytest.fail("should not be called"))

    redeem = make_trade(action="REDEEM", price=0.0, trade_id="r1")
    executor.execute(redeem, client=None, tracker=tracker, risk=risk)

    assert tracker.get("m1", paper=True) is None
    assert tracker.today_pnl_usdc(paper=True) == 6.0     # (1.0 - 0.40) * 10
