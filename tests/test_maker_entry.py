"""Maker entry — resting on the book instead of crossing it.

The money path here is different from every other one in the suite: an order
exists that is *not* a position and *not* a failure. These tests pin the three
things that go wrong if that distinction leaks — a phantom position, a
suspended market, or an order stacked on a market we are already working.

Real DB, real Ledger, real params. The CLOB client is the only stub.
"""

import time
from dataclasses import dataclass, field

import pytest

from bot.domain.intents import OpenIntent
from bot.domain.mode import Mode
from bot.execution import runner
from bot.execution.risk import RiskManager
from bot.storage import orders
from bot.storage.ledger import Ledger


@dataclass
class _MakerParams:
    name: str = "carry_maker"
    mode: object = Mode.PAPER
    max_position_size_usdc: float = 100.0
    max_total_exposure_usdc: float = 500.0
    daily_loss_limit_usdc: float = 50.0
    min_order_size_usdc: float = 0.5
    max_slippage: float = 0.5          # wide: these tests are not about drift
    paper_starting_balance: float = 10_000.0
    paper_fee_bps: float = 0.0
    poll_interval_seconds: int = 0
    order_type: str = "limit"
    entry_style: str = "maker"
    maker_ttl_seconds: float = 300.0
    settle_check_every: int = 999
    webhook_url: str = ""


class _Algo:
    def __init__(self, **kw):
        self.params = _MakerParams(**kw)
        self.name = self.params.name
        self.display_name = self.params.name


@pytest.fixture
def algo():
    return _Algo()


@pytest.fixture
def mk_ledger(fresh_db):
    led = Ledger(algo="carry_maker")
    led.init_paper_balance(1_000.0)
    return led


def _intent(bid=0.978, ask=0.985, market="0xm1"):
    return OpenIntent(
        market_id=market, asset_id="tok_yes", usdc_amount=1.0,
        signal_price=ask, question="Will Team A win?", outcome="Yes",
        signal_id=f"sig-{market}", features={"bid": bid, "ask": ask},
    )


def _dispatch(algo, mk_ledger, intent, price=None):
    """Run one OpenIntent through the runner in paper mode."""
    risk = RiskManager(mk_ledger, algo.params)
    runner.dispatch(intent, algo, mk_ledger, risk, None, paper=True)
    return risk


# ── resting ──────────────────────────────────────────────────────────────────

def test_a_maker_entry_rests_and_is_not_a_position(algo, mk_ledger, monkeypatch):
    """The core distinction: an order on the book is not a holding."""
    monkeypatch.setattr(runner, "_get_current_price", lambda t, c: 0.985)
    _dispatch(algo, mk_ledger, _intent())

    assert mk_ledger.get("0xm1", paper=True) is None, "resting order became a position"
    resting = orders.resting("carry_maker", paper=True)
    assert len(resting) == 1
    assert resting[0].limit_price < 0.985, "maker order crossed the spread"


def test_the_resting_price_never_crosses_the_ask(algo, mk_ledger, monkeypatch):
    """One tick of price improvement is free; a tick that would cross is not."""
    monkeypatch.setattr(runner, "_get_current_price", lambda t, c: 0.985)
    # bid and ask one tick apart: there is no room to improve, so sit on the bid
    _dispatch(algo, mk_ledger, _intent(bid=0.984, ask=0.985))
    assert orders.resting("carry_maker", paper=True)[0].limit_price == 0.984


def test_a_non_fill_does_not_suspend_the_market(algo, mk_ledger, monkeypatch):
    """A taker no-fill suspends the market until restart. For a maker,
    non-fills are the normal case and suspending would end the strategy after
    its first unfilled order."""
    monkeypatch.setattr(runner, "_get_current_price", lambda t, c: 0.985)
    risk = _dispatch(algo, mk_ledger, _intent())
    assert not risk.is_suspended("0xm1")


def test_resting_orders_hold_their_market_against_a_second_order(algo, mk_ledger, monkeypatch):
    """Polling every 15s against orders that take minutes to fill would stack
    duplicates on one market without this."""
    monkeypatch.setattr(runner, "_get_current_price", lambda t, c: 0.985)
    _dispatch(algo, mk_ledger, _intent())
    assert orders.held_market_ids("carry_maker", paper=True) == {"0xm1"}


# ── reconciliation ───────────────────────────────────────────────────────────

def test_paper_fills_only_when_the_market_trades_through_the_limit(
        algo, mk_ledger, monkeypatch):
    monkeypatch.setattr(runner, "_get_current_price", lambda t, c: 0.985)
    _dispatch(algo, mk_ledger, _intent())
    limit = orders.resting("carry_maker", paper=True)[0].limit_price

    # Still quoted above our bid — no fill.
    monkeypatch.setattr(runner, "_get_current_price", lambda t, c: limit + 0.002)
    runner.reconcile_open_orders(algo, mk_ledger, None, paper=True)
    assert mk_ledger.get("0xm1", paper=True) is None
    assert orders.count("carry_maker", paper=True) == 1

    # Someone sells into our bid.
    monkeypatch.setattr(runner, "_get_current_price", lambda t, c: limit)
    runner.reconcile_open_orders(algo, mk_ledger, None, paper=True)
    pos = mk_ledger.get("0xm1", paper=True)
    assert pos is not None and pos.shares > 0
    assert orders.count("carry_maker", paper=True) == 0, "filled order still resting"


def test_a_maker_fill_costs_the_limit_price_not_the_ask(algo, mk_ledger, monkeypatch):
    """The entire point: the ask was 0.985 and we paid the bid, with no fee."""
    monkeypatch.setattr(runner, "_get_current_price", lambda t, c: 0.985)
    _dispatch(algo, mk_ledger, _intent(bid=0.978, ask=0.985))
    limit = orders.resting("carry_maker", paper=True)[0].limit_price
    monkeypatch.setattr(runner, "_get_current_price", lambda t, c: limit)
    runner.reconcile_open_orders(algo, mk_ledger, None, paper=True)

    pos = mk_ledger.get("0xm1", paper=True)
    assert pos.avg_price == pytest.approx(limit)
    assert pos.avg_price < 0.985


def test_a_stale_order_is_dropped_and_leaves_no_position(algo, mk_ledger, monkeypatch):
    monkeypatch.setattr(runner, "_get_current_price", lambda t, c: 0.985)
    _dispatch(algo, mk_ledger, _intent())
    # Age the order past its TTL.
    algo.params = type(algo.params)(**{**algo.params.__dict__, "maker_ttl_seconds": 0.001})
    time.sleep(0.01)
    monkeypatch.setattr(runner, "_get_current_price", lambda t, c: 0.99)
    runner.reconcile_open_orders(algo, mk_ledger, None, paper=True)

    assert orders.count("carry_maker", paper=True) == 0
    assert mk_ledger.get("0xm1", paper=True) is None


def test_reconcile_is_a_no_op_for_a_taker_algorithm(mk_ledger, monkeypatch):
    """Every algorithm that predates this must be untouched by it."""
    taker = _Algo(entry_style="taker")
    monkeypatch.setattr(runner, "_get_current_price", lambda t, c: 0.985)
    risk = RiskManager(mk_ledger, taker.params)
    runner.dispatch(_intent(), taker, mk_ledger, risk, None, paper=True)

    # Took the ordinary paper path: a position, and nothing resting.
    assert mk_ledger.get("0xm1", paper=True) is not None
    assert orders.count("carry_maker", paper=True) == 0
    runner.reconcile_open_orders(taker, mk_ledger, None, paper=True)  # no crash


# ── the arm is wired ─────────────────────────────────────────────────────────

def test_the_variant_wires_the_maker_arm():
    from algorithms.resolution_carry.params import VARIANTS, ResolutionCarryParams

    p = ResolutionCarryParams(name="resolution_carry_maker_paper",
                              webhook_url="http://hook")
    p.validate()
    assert p.entry_style == "maker"
    # It must trade the same band as the control, or the arms are not comparable.
    control = ResolutionCarryParams(name="resolution_carry_paper",
                                    webhook_url="http://hook")
    assert (p.min_ask, p.max_ask) == (control.min_ask, control.max_ask)
    assert control.entry_style == "taker"
    assert "resolution_carry_maker_paper" in VARIANTS
