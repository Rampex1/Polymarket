"""
Live CLOB order placement — the delayed-order path and order sizing.

The CLOB matching engine is async: a small order can return status='delayed'
with empty amounts, resolving seconds later. Parsing that response as-is
reports a no-fill. On a BUY that just means a missed entry; on a SELL it
leaves a phantom position — the ledger keeps shares we no longer hold, the
paper balance never sees the proceeds, and live reconciliation drifts.

Both sides must therefore poll before parsing.
"""

import pytest

from tests.conftest import make_trade


class _FakeClob:
    """Minimal stand-in for ClobClient: records args, replays responses."""

    def __init__(self, post_resp, get_resp=None):
        self.post_resp = post_resp
        self.get_resp = get_resp
        self.market_args = []
        self.limit_args = []
        self.get_order_calls = 0

    def create_market_order(self, args):
        self.market_args.append(args)
        return "signed"

    def create_order(self, args):
        self.limit_args.append(args)
        return "signed"

    def post_order(self, signed, order_type):
        return self.post_resp

    def get_order(self, order_id):
        self.get_order_calls += 1
        return self.get_resp


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """_resolve_delayed waits between polls; don't make the suite wait."""
    import time
    monkeypatch.setattr(time, "sleep", lambda _s: None)


DELAYED = {"status": "delayed", "orderID": "0xorder1"}
MATCHED_SELL = {"status": "matched", "makingAmount": 20.0, "takingAmount": 14.0}
MATCHED_BUY = {"status": "matched", "makingAmount": 1.0, "takingAmount": 2.08}


# ── The bug this file exists for ────────────────────────────────────────────


def test_delayed_sell_is_polled_before_parsing():
    """A delayed SELL that later matches must be reported as a fill."""
    from bot.execution.runner import _place_sell

    client = _FakeClob(post_resp=DELAYED, get_resp=MATCHED_SELL)
    fill = _place_sell(make_trade(action="SELL"), 20.0, client, "market")

    assert client.get_order_calls >= 1
    assert fill.success
    assert fill.shares == 20.0
    assert fill.amount_usdc == 14.0


def test_delayed_buy_is_polled_before_parsing():
    from bot.execution.runner import _place_buy

    client = _FakeClob(post_resp=DELAYED, get_resp=MATCHED_BUY)
    fill = _place_buy(make_trade(action="BUY"), 1.0, client, "market")

    assert client.get_order_calls >= 1
    assert fill.success
    assert fill.shares == 2.08


def test_sell_still_delayed_after_polling_is_a_no_fill():
    """Fail closed: if it never resolves, do not claim a fill."""
    from bot.execution.runner import _place_sell

    client = _FakeClob(post_resp=DELAYED, get_resp=DELAYED)
    assert not _place_sell(make_trade(action="SELL"), 20.0, client, "market").success


# ── Order sizing: `amount` means different things per side ──────────────────


def test_market_orders_pass_amount_straight_through():
    """BUY spends USDC, SELL gives up shares — the CLOB takes what you give."""
    from bot.execution.runner import _place_buy, _place_sell

    buy = _FakeClob(post_resp=MATCHED_BUY)
    _place_buy(make_trade(action="BUY"), 5.0, buy, "market")
    assert buy.market_args[0].amount == 5.0
    assert buy.market_args[0].side == "BUY"

    sell = _FakeClob(post_resp=MATCHED_SELL)
    _place_sell(make_trade(action="SELL"), 20.0, sell, "market")
    assert sell.market_args[0].amount == 20.0
    assert sell.market_args[0].side == "SELL"


def test_limit_buy_converts_usdc_to_shares():
    """A limit order is sized in shares, but a BUY is requested in USDC."""
    from bot.execution.runner import _place_buy, _place_sell

    buy = _FakeClob(post_resp=MATCHED_BUY)
    _place_buy(make_trade(action="BUY", price=0.50), 5.0, buy, "limit")
    assert buy.limit_args[0].size == 10.0        # $5 / 0.50

    sell = _FakeClob(post_resp=MATCHED_SELL)
    _place_sell(make_trade(action="SELL", price=0.50), 20.0, sell, "limit")
    assert sell.limit_args[0].size == 20.0       # already shares


def test_order_failure_never_raises():
    """An SDK exception becomes a failed FillResult, not a crash mid-dispatch."""
    from bot.execution.runner import _place_buy

    class _Boom(_FakeClob):
        def post_order(self, signed, order_type):
            raise RuntimeError("connection reset")

    fill = _place_buy(make_trade(action="BUY"), 1.0, _Boom(post_resp=None), "market")
    assert not fill.success
    assert "connection reset" in fill.reason
