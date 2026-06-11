"""
Runner tests — slippage, simulated fills, fill parsing, and full
integration flows through `runner.dispatch()`.

The HTTP boundary (CLOB price, order placement, Polymarket resolution)
is stubbed. Risk checks, position recording, DB writes, and P&L
computation run through the real code paths.
"""

import pytest

from tests.conftest import make_trade

from bot.algorithm import CloseIntent, OpenIntent, SettleIntent


# ---------------------------------------------------------------------------
# A minimal stand-in Algorithm for the runner's dispatch — has .name and
# .params; the runner only reads those two attributes.
# ---------------------------------------------------------------------------


class _StubAlgo:
    def __init__(self, params):
        self.params = params

    @property
    def name(self):
        return self.params.name

    @property
    def display_name(self):
        return self.params.name


@pytest.fixture
def algo(default_params):
    return _StubAlgo(default_params)


# ---------------------------------------------------------------------------
# _slippage_ok — fail-safe semantics
# ---------------------------------------------------------------------------


def test_slippage_ok_within_tolerance(default_params):
    from bot.runner import _slippage_ok
    t = make_trade(price=0.50)
    assert _slippage_ok(t, current_price=0.52, max_slippage=0.05, algo_name="x")
    assert _slippage_ok(t, current_price=0.4751, max_slippage=0.05, algo_name="x")


def test_slippage_ok_rejects_excess_drift(default_params):
    from bot.runner import _slippage_ok
    t = make_trade(price=0.50)
    assert not _slippage_ok(t, current_price=0.60, max_slippage=0.05, algo_name="x")


def test_slippage_ok_with_zero_signal_price(default_params):
    """SettleIntent / forced-close trades carry price=0 — must not div-by-zero."""
    from bot.runner import _slippage_ok
    t = make_trade(price=0.0)
    assert _slippage_ok(t, current_price=0.5, max_slippage=0.05, algo_name="x")


def test_slippage_ok_refuses_when_price_unavailable(default_params):
    """Fail-closed: a None current_price defeats slippage protection only
    if we silently fill at the signal price. The runner refuses instead."""
    from bot.runner import _slippage_ok
    t = make_trade(price=0.50)
    assert not _slippage_ok(t, current_price=None, max_slippage=0.05, algo_name="x")


# ---------------------------------------------------------------------------
# Simulated fills — paper-mode realism
# ---------------------------------------------------------------------------


def test_simulate_buy_uses_current_price(default_params):
    from bot.runner import _simulate_buy
    fill = _simulate_buy(scaled_usdc=10.0, current_price=0.50, paper_fee_bps=0.0)
    assert fill.success
    assert fill.fill_price == 0.50
    assert fill.shares == 20.0
    assert fill.amount_usdc == 10.0


def test_simulate_buy_charges_fee(default_params):
    from bot.runner import _simulate_buy
    fill = _simulate_buy(scaled_usdc=100.0, current_price=0.50, paper_fee_bps=200.0)
    assert fill.fee_usdc == 2.0
    assert fill.shares == 98.0 / 0.50


def test_simulate_buy_rejects_when_price_none(default_params):
    from bot.runner import _simulate_buy
    fill = _simulate_buy(scaled_usdc=10.0, current_price=None, paper_fee_bps=0.0)
    assert not fill.success


def test_simulate_buy_rejects_when_fee_exceeds_size(default_params):
    """120% fee would otherwise silently produce negative shares."""
    from bot.runner import _simulate_buy
    fill = _simulate_buy(scaled_usdc=10.0, current_price=0.5, paper_fee_bps=12_000.0)
    assert not fill.success


def test_simulate_sell_proceeds_and_fee(default_params):
    from bot.runner import _simulate_sell
    fill = _simulate_sell(shares=100.0, current_price=0.70, paper_fee_bps=200.0)
    assert fill.amount_usdc == 70.0
    assert abs(fill.fee_usdc - 1.40) < 1e-9


# ---------------------------------------------------------------------------
# _parse_fill — order-response parsing
# ---------------------------------------------------------------------------


def test_parse_fill_rejected_response_returns_failure():
    from bot.runner import _parse_fill
    fill = _parse_fill({"success": False, "errorMsg": "no liquidity"}, side="BUY")
    assert not fill.success


def test_parse_fill_unmatched_status_returns_failure():
    from bot.runner import _parse_fill
    fill = _parse_fill({"status": "unmatched"}, side="BUY")
    assert not fill.success


def test_parse_fill_uses_actual_amounts_for_buy():
    from bot.runner import _parse_fill
    fill = _parse_fill(
        {"success": True, "takingAmount": 10.0, "makingAmount": 18.0},
        side="BUY",
    )
    assert fill.success
    assert fill.amount_usdc == 10.0
    assert fill.shares == 18.0
    assert abs(fill.fill_price - (10.0 / 18.0)) < 1e-9


def test_parse_fill_uses_actual_amounts_for_sell():
    from bot.runner import _parse_fill
    fill = _parse_fill(
        {"success": True, "takingAmount": 20.0, "makingAmount": 14.0},
        side="SELL",
    )
    assert fill.success
    assert fill.amount_usdc == 14.0
    assert fill.shares == 20.0


def test_parse_fill_accepts_alternative_field_names():
    from bot.runner import _parse_fill
    fill = _parse_fill(
        {"success": True, "taker_amount": "10.0", "maker_amount": "18.0"},
        side="BUY",
    )
    assert fill.success
    assert fill.amount_usdc == 10.0
    assert fill.shares == 18.0


def test_parse_fill_rejects_success_without_amounts():
    from bot.runner import _parse_fill
    fill = _parse_fill({"success": True}, side="BUY")
    assert not fill.success
    assert "no fill amounts" in fill.reason


def test_parse_fill_non_dict_returns_failure():
    from bot.runner import _parse_fill
    assert not _parse_fill("oops", side="BUY").success
    assert not _parse_fill(None, side="BUY").success


# ---------------------------------------------------------------------------
# Full dispatch() integration — paper mode
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_price(monkeypatch):
    """Pin runner._get_current_price for slippage + fill simulation."""
    from bot import runner

    def _set(value):
        monkeypatch.setattr(
            runner, "_get_current_price", lambda *a, **kw: value,
        )
    return _set


def test_dispatch_open_intent_paper(tracker, risk, algo, default_params, stub_price):
    from bot import runner
    stub_price(0.50)

    intent = OpenIntent(
        market_id="m1", asset_id="a1", usdc_amount=1.0, signal_price=0.50,
        question="Q?", outcome="Yes", signal_id="tx1", reason="tier 1",
    )
    runner.dispatch(intent, algo, tracker, risk, client=None, paper=True)

    p = tracker.get("m1", paper=True)
    assert p is not None
    assert p.total_cost_usdc == 1.0
    assert tracker.paper_balance() == 10_000.0 - 1.0


def test_dispatch_open_skipped_by_slippage(tracker, risk, algo, default_params, stub_price):
    from bot import runner
    stub_price(0.70)         # 40% drift from signal 0.50
    intent = OpenIntent(
        market_id="m1", asset_id="a1", usdc_amount=1.0, signal_price=0.50,
    )
    runner.dispatch(intent, algo, tracker, risk, client=None, paper=True)
    assert tracker.get("m1", paper=True) is None


def test_dispatch_open_skipped_when_price_fetch_fails(tracker, risk, algo,
                                                      default_params, stub_price):
    """If the current-price lookup returns None, slippage gate fails closed."""
    from bot import runner
    stub_price(None)
    intent = OpenIntent(
        market_id="m1", asset_id="a1", usdc_amount=1.0, signal_price=0.50,
    )
    runner.dispatch(intent, algo, tracker, risk, client=None, paper=True)
    assert tracker.get("m1", paper=True) is None


def test_dispatch_open_skipped_by_risk(tracker, risk, algo, default_params,
                                       monkeypatch, stub_price):
    """Risk check failure → no order placed."""
    from bot import runner
    monkeypatch.setattr(default_params, "min_order_size_usdc", 5.0)
    stub_price(0.50)
    intent = OpenIntent(
        market_id="m1", asset_id="a1", usdc_amount=1.0, signal_price=0.50,
    )
    runner.dispatch(intent, algo, tracker, risk, client=None, paper=True)
    assert tracker.get("m1", paper=True) is None


def test_dispatch_open_no_asset_id_is_skipped(tracker, risk, algo, default_params):
    from bot import runner
    intent = OpenIntent(
        market_id="m1", asset_id="", usdc_amount=1.0, signal_price=0.50,
    )
    runner.dispatch(intent, algo, tracker, risk, client=None, paper=True)
    assert tracker.get("m1", paper=True) is None


# ---------------------------------------------------------------------------
# CLOSE — fractional sell of held shares
# ---------------------------------------------------------------------------


def test_dispatch_close_partial(tracker, risk, algo, default_params, stub_price):
    """fraction=0.25 of 20 shares → 5 sold, 15 remain."""
    from bot import runner

    seed = make_trade(action="BUY", price=0.50)
    tracker.record_buy(seed, spent_usdc=10.0, shares=20.0, fill_price=0.50, paper=True)

    stub_price(0.50)

    intent = CloseIntent(
        market_id="m1", fraction=0.25, signal_price=0.50, outcome="Yes",
    )
    runner.dispatch(intent, algo, tracker, risk, client=None, paper=True)

    p = tracker.get("m1", paper=True)
    assert p is not None
    assert abs(p.shares - 15.0) < 1e-6
    assert p.avg_price == 0.50


def test_dispatch_close_full(tracker, risk, algo, default_params, stub_price):
    """fraction=1.0 closes the entire position."""
    from bot import runner

    seed = make_trade(action="BUY", price=0.50)
    tracker.record_buy(seed, spent_usdc=10.0, shares=20.0, fill_price=0.50, paper=True)

    stub_price(0.50)

    intent = CloseIntent(market_id="m1", fraction=1.0, signal_price=0.50)
    runner.dispatch(intent, algo, tracker, risk, client=None, paper=True)

    assert tracker.get("m1", paper=True) is None


def test_dispatch_close_no_position_is_noop(tracker, risk, algo, default_params, stub_price):
    from bot import runner
    stub_price(0.50)
    intent = CloseIntent(market_id="m1", fraction=1.0, signal_price=0.50)
    runner.dispatch(intent, algo, tracker, risk, client=None, paper=True)
    assert tracker.get("m1", paper=True) is None


def test_dispatch_close_zero_signal_price_skips_slippage(tracker, risk, algo,
                                                         default_params, stub_price):
    """A MERGE-style forced exit has signal_price=0.0 and must bypass the
    slippage gate. Current price still must be available."""
    from bot import runner

    seed = make_trade(action="BUY", price=0.50)
    tracker.record_buy(seed, spent_usdc=10.0, shares=20.0, fill_price=0.50, paper=True)

    # Pretend current price drifted way up — slippage would normally block.
    stub_price(0.95)

    intent = CloseIntent(market_id="m1", fraction=1.0, signal_price=0.0,
                         reason="target merged")
    runner.dispatch(intent, algo, tracker, risk, client=None, paper=True)
    assert tracker.get("m1", paper=True) is None


def test_dispatch_close_aborts_when_price_unavailable(tracker, risk, algo,
                                                      default_params, stub_price):
    """signal_price=0 + None current → must not attempt the fill."""
    from bot import runner

    seed = make_trade(action="BUY", price=0.50)
    tracker.record_buy(seed, spent_usdc=10.0, shares=20.0, fill_price=0.50, paper=True)

    stub_price(None)
    intent = CloseIntent(market_id="m1", fraction=1.0, signal_price=0.0)
    runner.dispatch(intent, algo, tracker, risk, client=None, paper=True)

    # Position untouched because we couldn't price the close.
    p = tracker.get("m1", paper=True)
    assert p is not None
    assert p.shares == 20.0


# ---------------------------------------------------------------------------
# SETTLE — resolved markets
# ---------------------------------------------------------------------------


def test_dispatch_settle_wins_at_one(tracker, risk, algo, default_params, monkeypatch):
    from bot import runner, fetcher
    seed = make_trade(action="BUY", price=0.40)
    tracker.record_buy(seed, spent_usdc=4.0, shares=10.0, fill_price=0.40, paper=True)

    monkeypatch.setattr(fetcher, "fetch_market_resolution", lambda *a, **kw: None)
    monkeypatch.setattr(fetcher, "fetch_resolution_price", lambda *a, **kw: 0.99)

    intent = SettleIntent(market_id="m1", signal_id="r1")
    runner.dispatch(intent, algo, tracker, risk, client=None, paper=True)

    assert tracker.get("m1", paper=True) is None
    assert tracker.today_pnl_usdc(paper=True) == 6.0


def test_dispatch_settle_loss_at_zero(tracker, risk, algo, default_params, monkeypatch):
    from bot import runner, fetcher
    seed = make_trade(action="BUY", price=0.40)
    tracker.record_buy(seed, spent_usdc=4.0, shares=10.0, fill_price=0.40, paper=True)

    monkeypatch.setattr(fetcher, "fetch_market_resolution", lambda *a, **kw: None)
    monkeypatch.setattr(fetcher, "fetch_resolution_price", lambda *a, **kw: 0.01)

    intent = SettleIntent(market_id="m1", signal_id="r1")
    runner.dispatch(intent, algo, tracker, risk, client=None, paper=True)
    assert tracker.get("m1", paper=True) is None
    assert tracker.today_pnl_usdc(paper=True) == -4.0


def test_dispatch_settle_ambiguous_leaves_open(tracker, risk, algo,
                                               default_params, monkeypatch):
    """Mid-range price + no Gamma confirmation → leave the position open."""
    from bot import runner, fetcher
    seed = make_trade(action="BUY", price=0.40)
    tracker.record_buy(seed, spent_usdc=4.0, shares=10.0, fill_price=0.40, paper=True)

    monkeypatch.setattr(fetcher, "fetch_market_resolution", lambda *a, **kw: None)
    monkeypatch.setattr(fetcher, "fetch_resolution_price", lambda *a, **kw: 0.50)

    intent = SettleIntent(market_id="m1", signal_id="r1")
    runner.dispatch(intent, algo, tracker, risk, client=None, paper=True)
    assert tracker.get("m1", paper=True) is not None


def test_dispatch_settle_uses_gamma_when_closed(tracker, risk, algo,
                                                default_params, monkeypatch):
    """Gamma's `closed` flag → trust outcomePrices."""
    from bot import runner, fetcher
    seed = make_trade(action="BUY", price=0.40, asset_id="winner")
    tracker.record_buy(seed, spent_usdc=4.0, shares=10.0, fill_price=0.40, paper=True)

    monkeypatch.setattr(
        fetcher, "fetch_market_resolution",
        lambda *a, **kw: {
            "closed": True,
            "clobTokenIds": '["winner", "loser"]',
            "outcomePrices": '["1.0", "0.0"]',
        },
    )
    monkeypatch.setattr(fetcher, "fetch_resolution_price",
                        lambda *a, **kw: pytest.fail("should not be called"))

    intent = SettleIntent(market_id="m1", signal_id="r1")
    runner.dispatch(intent, algo, tracker, risk, client=None, paper=True)
    assert tracker.get("m1", paper=True) is None
    assert tracker.today_pnl_usdc(paper=True) == 6.0


def test_dispatch_settle_ignores_gamma_when_open(tracker, risk, algo,
                                                 default_params, monkeypatch):
    """Live mid (not a settlement) → fall back to CLOB binarisation."""
    from bot import runner, fetcher
    seed = make_trade(action="BUY", price=0.40, asset_id="winner")
    tracker.record_buy(seed, spent_usdc=4.0, shares=10.0, fill_price=0.40, paper=True)

    monkeypatch.setattr(
        fetcher, "fetch_market_resolution",
        lambda *a, **kw: {
            "closed": False,
            "clobTokenIds": '["winner", "loser"]',
            "outcomePrices": '["0.42", "0.58"]',
        },
    )
    monkeypatch.setattr(fetcher, "fetch_resolution_price", lambda *a, **kw: 0.99)

    intent = SettleIntent(market_id="m1", signal_id="r1")
    runner.dispatch(intent, algo, tracker, risk, client=None, paper=True)
    assert tracker.get("m1", paper=True) is None
    assert tracker.today_pnl_usdc(paper=True) == 6.0


def test_market_is_resolved_accepts_alternate_flags():
    # Lives in bot.fetcher (not runner) so paper-mode algorithms can use it
    # without transitively importing py_clob_client.
    from bot.fetcher import market_is_resolved
    assert market_is_resolved({"closed": True})
    assert market_is_resolved({"resolved": True})
    assert market_is_resolved({"archived": True})
    assert market_is_resolved({"closed": "true"})
    assert not market_is_resolved({})
    assert not market_is_resolved({"closed": False, "resolved": False})
