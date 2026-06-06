"""
RiskManager tests — gates, escalation order, sell bypass.

The single most important test in this file is `test_sell_bypasses_daily_loss`:
the pre-fix bot would block a SELL when down on the day, locking itself into
losing positions. We make sure that can never happen again.
"""

from tests.conftest import make_trade


# ---------------------------------------------------------------------------
# Sells / redeems unconditionally pass
# ---------------------------------------------------------------------------


def test_sell_bypasses_daily_loss(risk, tracker, default_config):
    """The critical fix: a SELL must not be blocked by the daily-loss limit."""
    # Engineer a loss large enough to trip the limit.
    buy = make_trade(action="BUY", price=0.5)
    tracker.record_buy(buy, spent_usdc=100.0, shares=200.0, fill_price=0.5, paper=True)
    big_sell = make_trade(action="SELL", price=0.1, trade_id="lossy")
    tracker.record_sell(big_sell, shares=200.0, proceeds_usdc=20.0,
                        fill_price=0.1, paper=True)
    # Now today_pnl is -80, below the limit.
    assert tracker.today_pnl_usdc(paper=True) < -default_config.DAILY_LOSS_LIMIT_USDC

    sell = make_trade(action="SELL", price=0.5, trade_id="exit")
    ok, reason = risk.check(sell, 0, paper=True)
    assert ok, f"SELL should never be blocked, got: {reason}"


def test_redeem_bypasses_all_checks(risk, default_config):
    redeem = make_trade(action="REDEEM", price=0.0, trade_id="r1")
    ok, _ = risk.check(redeem, 0, paper=True)
    assert ok


# ---------------------------------------------------------------------------
# BUY gates — in declared escalation order
# ---------------------------------------------------------------------------


def test_buy_blocked_below_min_order_size(risk, default_config):
    t = make_trade(action="BUY")
    ok, reason = risk.check(t, 0.50, paper=True)
    assert not ok
    assert "below min" in reason


def test_buy_blocked_by_daily_loss(risk, tracker, default_config):
    # Engineer a day loss.
    buy = make_trade(action="BUY", price=0.5)
    tracker.record_buy(buy, spent_usdc=100.0, shares=200.0, fill_price=0.5, paper=True)
    sell = make_trade(action="SELL", price=0.1, trade_id="lossy")
    tracker.record_sell(sell, shares=200.0, proceeds_usdc=20.0, fill_price=0.1, paper=True)

    next_buy = make_trade(action="BUY", market_id="m2")
    ok, reason = risk.check(next_buy, 5.0, paper=True)
    assert not ok
    assert "Daily loss" in reason


def test_buy_blocked_by_position_size(risk, tracker, default_config, monkeypatch):
    monkeypatch.setattr(default_config, "MAX_POSITION_SIZE_USDC", 5.0)
    buy = make_trade(action="BUY", price=0.5)
    tracker.record_buy(buy, spent_usdc=4.0, shares=8.0, fill_price=0.5, paper=True)
    t = make_trade(action="BUY", trade_id="tx2")
    ok, reason = risk.check(t, 5.0, paper=True)   # would push to $9 > $5 cap
    assert not ok
    assert "Position size" in reason


def test_buy_blocked_by_total_exposure(risk, tracker, default_config, monkeypatch):
    monkeypatch.setattr(default_config, "MAX_TOTAL_EXPOSURE_USDC", 10.0)
    monkeypatch.setattr(default_config, "MAX_POSITION_SIZE_USDC", 100.0)
    # Two positions adding up to $9 already.
    b1 = make_trade(action="BUY", market_id="m1", price=0.5)
    b2 = make_trade(action="BUY", market_id="m2", price=0.5)
    tracker.record_buy(b1, 5.0, 10.0, 0.5, paper=True)
    tracker.record_buy(b2, 4.0, 8.0, 0.5, paper=True)
    t = make_trade(action="BUY", market_id="m3")
    ok, reason = risk.check(t, 5.0, paper=True)   # 9 + 5 = 14 > 10
    assert not ok
    assert "Total exposure" in reason


def test_buy_blocked_by_paper_balance(risk, default_config):
    t = make_trade(action="BUY")
    # Cap balance at 5, request 6.
    risk.tracker._adjust_paper_balance(-(10_000.0 - 5.0))   # leave 5
    import bot.db as db
    db.get().commit()
    ok, reason = risk.check(t, 6.0, paper=True)
    assert not ok
    assert "Insufficient paper balance" in reason


def test_paper_balance_not_checked_in_live(risk, default_config):
    """Live mode delegates balance enforcement to the exchange."""
    t = make_trade(action="BUY")
    # Even with $0 paper balance, live BUY should not be balance-blocked.
    risk.tracker._adjust_paper_balance(-10_000.0)
    import bot.db as db
    db.get().commit()
    ok, _ = risk.check(t, 5.0, paper=False)
    assert ok


def test_happy_path_buy_approved(risk, default_config):
    t = make_trade(action="BUY")
    ok, reason = risk.check(t, 5.0, paper=True)
    assert ok, reason
