"""
Ledger tests — math, accounting, edge cases.

These exercise the real SQLite DB. The bugs fixed in the senior review
that this file pins down:
  * Cost basis comes from actual fills, not the target's signal price.
  * Partial sells preserve avg_price and scale total_cost_usdc.
  * Fees flow through paper P&L and balance accounting.
  * record_buy refuses to record a phantom position when shares/price≤0.
"""

from tests.conftest import make_trade


# ---------------------------------------------------------------------------
# Paper balance
# ---------------------------------------------------------------------------


def test_init_paper_balance_is_idempotent(tracker):
    """Second call must not overwrite an existing balance."""
    tracker.init_paper_balance(10_000.0)
    assert tracker.paper_balance() == 10_000.0
    tracker.init_paper_balance(999.0)
    assert tracker.paper_balance() == 10_000.0


# ---------------------------------------------------------------------------
# Buy → position math
# ---------------------------------------------------------------------------


def test_record_buy_creates_position(tracker):
    t = make_trade(action="BUY", price=0.4)
    tracker.record_buy(t, spent_usdc=10.0, shares=25.0, fill_price=0.4, paper=True)
    p = tracker.get("m1", paper=True)
    assert p is not None
    assert p.shares == 25.0
    assert p.avg_price == 0.4
    assert p.total_cost_usdc == 10.0


def test_record_buy_dca_updates_avg_price(tracker):
    """Two buys at different prices → weighted average."""
    t1 = make_trade(action="BUY", price=0.4)
    t2 = make_trade(action="BUY", price=0.6)
    tracker.record_buy(t1, spent_usdc=10.0, shares=25.0, fill_price=0.4, paper=True)
    tracker.record_buy(t2, spent_usdc=10.0, shares=16.66666, fill_price=0.6, paper=True)
    p = tracker.get("m1", paper=True)
    expected_shares = 25.0 + 16.66666
    expected_avg = 20.0 / expected_shares     # total cost $20 / shares
    assert p.shares == expected_shares
    assert p.avg_price == expected_avg
    assert p.total_cost_usdc == 20.0


def test_record_buy_refuses_zero_shares(tracker, caplog):
    """Phantom-position guard — caller has already 'placed' the order, but
    we refuse to write garbage into cost basis math."""
    t = make_trade(action="BUY")
    tracker.record_buy(t, spent_usdc=10.0, shares=0.0, fill_price=0.5, paper=True)
    assert tracker.get("m1", paper=True) is None


def test_record_buy_paper_subtracts_balance_with_fee(tracker):
    t = make_trade(action="BUY", price=0.4)
    tracker.record_buy(t, spent_usdc=10.0, shares=25.0, fill_price=0.4,
                      paper=True, fee_usdc=0.5)
    # Paper balance starts at 10000, spent 10 + fee 0.5.
    assert tracker.paper_balance() == 10_000.0 - 10.5


def test_record_buy_live_does_not_touch_paper_balance(tracker):
    t = make_trade(action="BUY")
    tracker.record_buy(t, spent_usdc=10.0, shares=25.0, fill_price=0.4, paper=False)
    assert tracker.paper_balance() == 10_000.0


# ---------------------------------------------------------------------------
# Sell → P&L + partial-close math
# ---------------------------------------------------------------------------


def test_record_sell_full_close_pnl_and_balance(tracker):
    buy = make_trade(action="BUY", price=0.4)
    tracker.record_buy(buy, spent_usdc=10.0, shares=25.0, fill_price=0.4, paper=True)

    sell = make_trade(action="SELL", price=0.6, trade_id="tx2")
    tracker.record_sell(sell, shares=25.0, proceeds_usdc=15.0,
                        fill_price=0.6, paper=True)

    # Position should be removed entirely.
    assert tracker.get("m1", paper=True) is None
    # P&L: proceeds 15 - cost 10 = 5
    assert tracker.today_pnl_usdc(paper=True) == 5.0
    # Balance: 10000 - 10 + 15 = 10005
    assert tracker.paper_balance() == 10_005.0


def test_record_sell_partial_preserves_avg_price(tracker):
    """Critical financial-logic test: a partial close must NOT change avg_price.
    Cost basis for remaining shares scales linearly."""
    buy = make_trade(action="BUY", price=0.4)
    tracker.record_buy(buy, spent_usdc=10.0, shares=25.0, fill_price=0.4, paper=True)

    sell = make_trade(action="SELL", price=0.6, trade_id="tx2")
    tracker.record_sell(sell, shares=10.0, proceeds_usdc=6.0,
                        fill_price=0.6, paper=True)

    p = tracker.get("m1", paper=True)
    assert p is not None
    assert p.shares == 15.0                         # 25 - 10
    assert p.avg_price == 0.4                       # preserved
    assert abs(p.total_cost_usdc - 6.0) < 1e-9      # 15 * 0.4


def test_record_sell_realized_pnl_includes_fee(tracker):
    """P&L should be (proceeds - fee) - cost basis, not gross proceeds - cost."""
    buy = make_trade(action="BUY", price=0.4)
    tracker.record_buy(buy, spent_usdc=10.0, shares=25.0, fill_price=0.4, paper=True)

    sell = make_trade(action="SELL", price=0.6, trade_id="tx2")
    tracker.record_sell(sell, shares=25.0, proceeds_usdc=15.0,
                        fill_price=0.6, paper=True, fee_usdc=0.30)

    # Gross P&L $5 - fee $0.30 = $4.70
    assert abs(tracker.today_pnl_usdc(paper=True) - 4.70) < 1e-9
    # Balance: 10000 - 10 + (15 - 0.30) = 10004.70
    assert abs(tracker.paper_balance() - 10_004.70) < 1e-9


def test_paper_and_live_positions_are_isolated(tracker):
    buy_p = make_trade(action="BUY", price=0.4, trade_id="p1")
    buy_l = make_trade(action="BUY", price=0.4, trade_id="l1")
    tracker.record_buy(buy_p, 10.0, 25.0, 0.4, paper=True)
    tracker.record_buy(buy_l, 20.0, 50.0, 0.4, paper=False)

    pp = tracker.get("m1", paper=True)
    pl = tracker.get("m1", paper=False)
    assert pp.shares == 25.0
    assert pl.shares == 50.0
    # Exposure must split correctly.
    assert tracker.total_exposure_usdc(paper=True) == 10.0
    assert tracker.total_exposure_usdc(paper=False) == 20.0
    assert tracker.total_exposure_usdc(paper=None) == 30.0


def test_total_cost_usdc_is_canonical_cost_basis(tracker):
    """`total_cost_usdc` is the single source of truth for cost basis.
    The earlier `cost_basis_usdc` property was removed because it could drift
    from this value under floating-point rounding on partial sells."""
    buy = make_trade(action="BUY", price=0.4)
    tracker.record_buy(buy, spent_usdc=10.0, shares=25.0, fill_price=0.4, paper=True)
    p = tracker.get("m1", paper=True)
    assert p.total_cost_usdc == 10.0
    # The property no longer exists — accessing it should AttributeError.
    import pytest
    with pytest.raises(AttributeError):
        _ = p.cost_basis_usdc


def test_today_pnl_filters_by_paper_flag(tracker):
    """today_pnl_usdc(paper=…) must isolate paper from live numbers."""
    buy_p = make_trade(action="BUY", price=0.4)
    tracker.record_buy(buy_p, 10.0, 25.0, 0.4, paper=True)
    sell_p = make_trade(action="SELL", price=0.6, trade_id="ps")
    tracker.record_sell(sell_p, 25.0, 15.0, 0.6, paper=True)

    buy_l = make_trade(action="BUY", market_id="m2", price=0.4)
    tracker.record_buy(buy_l, 10.0, 25.0, 0.4, paper=False)
    sell_l = make_trade(action="SELL", market_id="m2", price=0.3, trade_id="ls")
    tracker.record_sell(sell_l, 25.0, 7.5, 0.3, paper=False)

    assert tracker.today_pnl_usdc(paper=True) == 5.0
    assert tracker.today_pnl_usdc(paper=False) == -2.5
    assert tracker.today_pnl_usdc(paper=None) == 2.5
