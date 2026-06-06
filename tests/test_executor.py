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
    assert _tier_for_holding(80_000) == 1.0
    assert _tier_for_holding(150_000) == 1.0       # inclusive boundary
    assert _tier_for_holding(150_001) == 2.0
    assert _tier_for_holding(300_000) == 2.0       # inclusive boundary
    assert _tier_for_holding(300_001) == 3.0
    assert _tier_for_holding(1_000_000) == 3.0


# ---------------------------------------------------------------------------
# _slippage_ok — fail-safe semantics
# ---------------------------------------------------------------------------


def test_slippage_ok_within_tolerance(default_config):
    from bot.executor import _slippage_ok
    t = make_trade(price=0.50)
    assert _slippage_ok(t, current_price=0.52)
    assert _slippage_ok(t, current_price=0.4751)


def test_slippage_ok_rejects_excess_drift(default_config):
    from bot.executor import _slippage_ok
    t = make_trade(price=0.50)
    assert not _slippage_ok(t, current_price=0.60)


def test_slippage_ok_with_zero_signal_price(default_config):
    """REDEEM signals carry price=0 — must not div-by-zero."""
    from bot.executor import _slippage_ok
    t = make_trade(price=0.0)
    assert _slippage_ok(t, current_price=0.5)


def test_slippage_ok_refuses_when_price_unavailable(default_config):
    """Failure mode that matters: when the price lookup fails (returns None),
    we MUST refuse the trade. Pre-fix the code silently filled at signal
    price, defeating slippage protection during the exact outages where
    it's most needed."""
    from bot.executor import _slippage_ok
    t = make_trade(price=0.50)
    assert not _slippage_ok(t, current_price=None)


# ---------------------------------------------------------------------------
# Simulated fills — paper-mode realism
# ---------------------------------------------------------------------------


def test_simulate_buy_uses_current_price(default_config):
    """Paper fill must use the *current* price, not the target's signal price."""
    from bot.executor import _simulate_buy
    t = make_trade(price=0.40)
    fill = _simulate_buy(t, scaled_usdc=10.0, current_price=0.50)
    assert fill.success
    assert fill.fill_price == 0.50
    assert fill.shares == 20.0
    assert fill.amount_usdc == 10.0


def test_simulate_buy_charges_fee(default_config, monkeypatch):
    from bot.executor import _simulate_buy
    monkeypatch.setattr(default_config, "PAPER_FEE_BPS", 200.0)   # 2%
    t = make_trade()
    fill = _simulate_buy(t, scaled_usdc=100.0, current_price=0.50)
    assert fill.fee_usdc == 2.0
    assert fill.shares == 98.0 / 0.50          # 196 shares


def test_simulate_buy_rejects_when_price_none(default_config):
    from bot.executor import _simulate_buy
    t = make_trade()
    fill = _simulate_buy(t, scaled_usdc=10.0, current_price=None)
    assert not fill.success


def test_simulate_buy_rejects_when_fee_exceeds_size(default_config, monkeypatch):
    """Defensive: misconfigured fee bps (e.g. 12000 = 120%) would otherwise
    silently produce negative shares."""
    from bot.executor import _simulate_buy
    monkeypatch.setattr(default_config, "PAPER_FEE_BPS", 12_000.0)
    t = make_trade()
    fill = _simulate_buy(t, scaled_usdc=10.0, current_price=0.5)
    assert not fill.success


def test_simulate_sell_proceeds_and_fee(default_config, monkeypatch):
    from bot.executor import _simulate_sell
    monkeypatch.setattr(default_config, "PAPER_FEE_BPS", 200.0)
    t = make_trade(action="SELL")
    fill = _simulate_sell(t, shares=100.0, current_price=0.70)
    assert fill.amount_usdc == 70.0
    assert abs(fill.fee_usdc - 1.40) < 1e-9


# ---------------------------------------------------------------------------
# _parse_fill — order-response parsing
# ---------------------------------------------------------------------------


def test_parse_fill_rejected_response_returns_failure():
    from bot.executor import _parse_fill
    fill = _parse_fill({"success": False, "errorMsg": "no liquidity"}, side="BUY")
    assert not fill.success


def test_parse_fill_unmatched_status_returns_failure():
    from bot.executor import _parse_fill
    fill = _parse_fill({"status": "unmatched"}, side="BUY")
    assert not fill.success


def test_parse_fill_uses_actual_amounts_for_buy():
    """Cost basis must come from the response, not a request-derived guess."""
    from bot.executor import _parse_fill
    fill = _parse_fill(
        {"success": True, "takingAmount": 10.0, "makingAmount": 18.0},
        side="BUY",
    )
    assert fill.success
    assert fill.amount_usdc == 10.0
    assert fill.shares == 18.0
    assert abs(fill.fill_price - (10.0 / 18.0)) < 1e-9


def test_parse_fill_uses_actual_amounts_for_sell():
    from bot.executor import _parse_fill
    fill = _parse_fill(
        {"success": True, "takingAmount": 20.0, "makingAmount": 14.0},
        side="SELL",
    )
    assert fill.success
    assert fill.amount_usdc == 14.0          # proceeds
    assert fill.shares == 20.0


def test_parse_fill_accepts_alternative_field_names():
    """py_clob_client may surface either takingAmount or taker_amount —
    we accept both since the canonical name isn't pinned down."""
    from bot.executor import _parse_fill
    fill = _parse_fill(
        {"success": True, "taker_amount": "10.0", "maker_amount": "18.0"},
        side="BUY",
    )
    assert fill.success
    assert fill.amount_usdc == 10.0
    assert fill.shares == 18.0


def test_parse_fill_rejects_success_without_amounts():
    """Conservative posture: success=True with no parseable amounts is treated
    as failure. Otherwise we'd record phantom positions when the API returns
    an async ack without fill data."""
    from bot.executor import _parse_fill
    fill = _parse_fill({"success": True}, side="BUY")
    assert not fill.success
    assert "no fill amounts" in fill.reason


def test_parse_fill_non_dict_returns_failure():
    from bot.executor import _parse_fill
    assert not _parse_fill("oops", side="BUY").success
    assert not _parse_fill(None, side="BUY").success


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
    from bot import executor

    stub_holding(100_000)          # tier1
    stub_price(0.50)               # no slippage

    t = make_trade(action="BUY", price=0.50)
    executor.execute(t, client=None, tracker=tracker, risk=risk)

    p = tracker.get("m1", paper=True)
    assert p is not None
    assert p.total_cost_usdc == 1.0
    assert tracker.paper_balance() == 10_000.0 - 1.0


def test_execute_buy_populates_holding_cache(tracker, risk, default_config,
                                              stub_holding, stub_price):
    """BUY path must seed the target-holding cache so the SELL path has
    a pre-trade value to mirror against."""
    from bot import executor, fetcher

    stub_holding(100_000)
    stub_price(0.50)

    t = make_trade(action="BUY")
    executor.execute(t, client=None, tracker=tracker, risk=risk)

    assert fetcher.target_holding_cache.get("m1") == 100_000


def test_execute_buy_skipped_below_tier1(tracker, risk, default_config, stub_holding, stub_price):
    from bot import executor

    stub_holding(50_000)
    stub_price(0.50)
    t = make_trade(action="BUY")
    executor.execute(t, client=None, tracker=tracker, risk=risk)
    assert tracker.get("m1", paper=True) is None


def test_execute_buy_skipped_by_slippage(tracker, risk, default_config, stub_holding, stub_price):
    from bot import executor

    stub_holding(100_000)
    stub_price(0.70)                # 40% drift

    t = make_trade(action="BUY", price=0.50)
    executor.execute(t, client=None, tracker=tracker, risk=risk)
    assert tracker.get("m1", paper=True) is None


def test_execute_buy_skipped_by_min_order(tracker, risk, default_config,
                                          stub_holding, stub_price, monkeypatch):
    from bot import executor

    monkeypatch.setattr(default_config, "TIER1_SIZE", 0.5)
    monkeypatch.setattr(default_config, "MIN_ORDER_SIZE_USDC", 1.0)

    stub_holding(100_000)
    stub_price(0.50)
    t = make_trade(action="BUY")
    executor.execute(t, client=None, tracker=tracker, risk=risk)
    assert tracker.get("m1", paper=True) is None


def test_execute_buy_skipped_when_price_fetch_fails(tracker, risk, default_config,
                                                    stub_holding, stub_price):
    """If the current-price lookup returns None, the slippage gate fails closed
    — no order, no position."""
    from bot import executor

    stub_holding(100_000)
    stub_price(None)
    t = make_trade(action="BUY", price=0.50)
    executor.execute(t, client=None, tracker=tracker, risk=risk)
    assert tracker.get("m1", paper=True) is None


# ---------------------------------------------------------------------------
# SELL — cache-driven proportional close
# ---------------------------------------------------------------------------


def test_execute_sell_uses_cached_pre_holding(tracker, risk, default_config,
                                              stub_price, monkeypatch):
    """The headline behavior: a SELL with a known pre-trade holding closes
    exactly the cached_ratio of our shares."""
    from bot import executor, fetcher

    # Seed position and cache to known values.
    seed = make_trade(action="BUY", price=0.50)
    tracker.record_buy(seed, spent_usdc=10.0, shares=20.0, fill_price=0.50, paper=True)
    fetcher.target_holding_cache.set("m1", 100_000.0)

    stub_price(0.50)

    # Target sells $25k of their $100k → 25% close ratio.
    sell = make_trade(action="SELL", price=0.50, size_usdc=25_000.0, trade_id="s1")
    executor.execute(sell, client=None, tracker=tracker, risk=risk)

    p = tracker.get("m1", paper=True)
    assert p is not None
    assert abs(p.shares - 15.0) < 1e-6           # 75% of 20
    assert p.avg_price == 0.50

    # Cache is decremented for subsequent partials.
    assert abs(fetcher.target_holding_cache.get("m1") - 75_000.0) < 1e-6


def test_execute_sell_cache_miss_falls_back_to_full_close(tracker, risk, default_config,
                                                          stub_price):
    """With no cached pre-trade value (e.g. first signal since startup),
    we full-close. Safer than guessing."""
    from bot import executor

    seed = make_trade(action="BUY", price=0.50)
    tracker.record_buy(seed, spent_usdc=10.0, shares=20.0, fill_price=0.50, paper=True)

    stub_price(0.50)

    sell = make_trade(action="SELL", price=0.50, size_usdc=25_000.0, trade_id="s1")
    executor.execute(sell, client=None, tracker=tracker, risk=risk)

    # Position fully closed.
    assert tracker.get("m1", paper=True) is None


def test_execute_sell_does_not_addand_postsell_holding(tracker, risk, default_config,
                                                       stub_price, monkeypatch):
    """Pre-fix bug: the ratio was reconstructed as `(post_sell_holding +
    size_usdc) / size_usdc`, which is mathematically wrong because holding
    is mark-to-market USD and size_usdc is the fill notional. We DO NOT
    fetch the post-sell holding anymore — the cache is the source of truth.

    Concretely: even if the fetch function would return wildly wrong data
    (e.g. zero, or stale pre-sell value), the cache-driven path doesn't
    care and computes the correct ratio.
    """
    from bot import executor, fetcher

    seed = make_trade(action="BUY", price=0.50)
    tracker.record_buy(seed, spent_usdc=10.0, shares=20.0, fill_price=0.50, paper=True)
    fetcher.target_holding_cache.set("m1", 100_000.0)

    # Make the API stub return absurd values — the SELL path must IGNORE it.
    monkeypatch.setattr(
        fetcher, "fetch_target_position_value",
        lambda *a, **kw: pytest.fail("SELL path must not call fetch_target_position_value"),
    )
    stub_price(0.50)

    sell = make_trade(action="SELL", price=0.50, size_usdc=10_000.0, trade_id="s1")
    executor.execute(sell, client=None, tracker=tracker, risk=risk)

    p = tracker.get("m1", paper=True)
    # 10k / 100k = 10% close → 2 of 20 shares sold → 18 remain.
    assert abs(p.shares - 18.0) < 1e-6


def test_execute_sell_with_no_position_is_noop(tracker, risk, default_config, stub_price):
    from bot import executor
    stub_price(0.50)
    sell = make_trade(action="SELL", price=0.50)
    executor.execute(sell, client=None, tracker=tracker, risk=risk)
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
    """CLOB last-trade near 1.0 → WIN, position closes at 1.0."""
    from bot import executor, fetcher

    seed = make_trade(action="BUY", price=0.40)
    tracker.record_buy(seed, spent_usdc=4.0, shares=10.0, fill_price=0.40, paper=True)

    monkeypatch.setattr(fetcher, "fetch_market_resolution", lambda *a, **kw: None)
    monkeypatch.setattr(fetcher, "fetch_resolution_price", lambda *a, **kw: 0.99)

    redeem = make_trade(action="REDEEM", price=0.0, trade_id="r1")
    executor.execute(redeem, client=None, tracker=tracker, risk=risk)

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
    assert tracker.today_pnl_usdc(paper=True) == -4.0


def test_redeem_with_ambiguous_price_leaves_position_open(tracker, risk, default_config, monkeypatch):
    from bot import executor, fetcher

    seed = make_trade(action="BUY", price=0.40)
    tracker.record_buy(seed, spent_usdc=4.0, shares=10.0, fill_price=0.40, paper=True)

    monkeypatch.setattr(fetcher, "fetch_market_resolution", lambda *a, **kw: None)
    monkeypatch.setattr(fetcher, "fetch_resolution_price", lambda *a, **kw: 0.50)

    redeem = make_trade(action="REDEEM", price=0.0, trade_id="r1")
    executor.execute(redeem, client=None, tracker=tracker, risk=risk)

    assert tracker.get("m1", paper=True) is not None


def test_redeem_uses_gamma_resolution_when_market_is_closed(tracker, risk, default_config, monkeypatch):
    """If Gamma confirms the market is closed/resolved, trust outcomePrices."""
    from bot import executor, fetcher

    seed = make_trade(action="BUY", price=0.40, asset_id="winner")
    tracker.record_buy(seed, spent_usdc=4.0, shares=10.0, fill_price=0.40, paper=True)

    monkeypatch.setattr(
        fetcher, "fetch_market_resolution",
        lambda *a, **kw: {
            "closed": True,             # the critical flag
            "clobTokenIds": '["winner", "loser"]',
            "outcomePrices": '["1.0", "0.0"]',
        },
    )
    monkeypatch.setattr(fetcher, "fetch_resolution_price",
                        lambda *a, **kw: pytest.fail("should not be called"))

    redeem = make_trade(action="REDEEM", price=0.0, trade_id="r1")
    executor.execute(redeem, client=None, tracker=tracker, risk=risk)

    assert tracker.get("m1", paper=True) is None
    assert tracker.today_pnl_usdc(paper=True) == 6.0


def test_redeem_ignores_gamma_when_market_not_closed(tracker, risk, default_config, monkeypatch):
    """Critical fix: a *live* market returning outcomePrices like ['0.42','0.58']
    must NOT be used to settle a redemption. The closed flag gates the
    Gamma path; otherwise fall back to CLOB last-trade-price."""
    from bot import executor, fetcher

    seed = make_trade(action="BUY", price=0.40, asset_id="winner")
    tracker.record_buy(seed, spent_usdc=4.0, shares=10.0, fill_price=0.40, paper=True)

    monkeypatch.setattr(
        fetcher, "fetch_market_resolution",
        lambda *a, **kw: {
            "closed": False,             # market is NOT resolved
            "clobTokenIds": '["winner", "loser"]',
            "outcomePrices": '["0.42", "0.58"]',   # live mid, not settlement
        },
    )
    monkeypatch.setattr(fetcher, "fetch_resolution_price", lambda *a, **kw: 0.99)

    redeem = make_trade(action="REDEEM", price=0.0, trade_id="r1")
    executor.execute(redeem, client=None, tracker=tracker, risk=risk)

    # Should have closed at 1.0 (the CLOB fallback), NOT at 0.42 (the live mid).
    assert tracker.get("m1", paper=True) is None
    assert tracker.today_pnl_usdc(paper=True) == 6.0


def test_market_is_resolved_accepts_alternate_flags(default_config):
    """Gamma's flag names have varied — accept any of closed/resolved/archived."""
    from bot.executor import _market_is_resolved
    assert _market_is_resolved({"closed": True})
    assert _market_is_resolved({"resolved": True})
    assert _market_is_resolved({"archived": True})
    # String forms also accepted (some API versions stringify).
    assert _market_is_resolved({"closed": "true"})
    # All-falsy or absent flags → not resolved.
    assert not _market_is_resolved({})
    assert not _market_is_resolved({"closed": False, "resolved": False})
