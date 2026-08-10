"""
Ledger writes are all-or-nothing.

Recording a trade touches three tables: positions, trade_log, and (in paper)
paper_account. They must land together. The connection is thread-local and
reused, so an exception partway through leaves statements pending — and
main.py's poll loop catches everything and keeps running, meaning the next
trade's commit would flush the half-written one.

Symptom if this regresses: a position with no trade_log row and no cash
debit — exposure counting money that was never spent.
"""

import pytest

from tests.conftest import make_trade


class _FailOnce:
    """Wraps the connection; raises the first time a statement matches `on`."""

    def __init__(self, conn, on: str):
        self._conn = conn
        self._on = on
        self.fired = False

    def execute(self, sql, *a, **kw):
        if self._on in sql and not self.fired:
            self.fired = True
            raise RuntimeError("simulated mid-write failure")
        return self._conn.execute(sql, *a, **kw)

    def __enter__(self):
        return self._conn.__enter__()

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._conn, name)


@pytest.fixture
def break_on(monkeypatch, fresh_db):
    """Make the next ledger write blow up on a chosen statement."""
    real = fresh_db.get()

    def _set(fragment: str):
        wrapped = _FailOnce(real, fragment)
        monkeypatch.setattr(fresh_db, "get", lambda: wrapped)
    return _set


def test_failed_buy_leaves_no_position(ledger, break_on, fresh_db):
    """positions is written before trade_log — it must not survive alone."""
    start = ledger.paper_balance()

    break_on("INSERT INTO trade_log")
    with pytest.raises(RuntimeError):
        ledger.record_buy(make_trade(market_id="m1"), spent_usdc=5.0, shares=10.0,
                          fill_price=0.5, paper=True)

    # A later, successful write must not drag the orphan in with it.
    ledger.record_buy(make_trade(market_id="m2", trade_id="tx2"), spent_usdc=5.0,
                      shares=10.0, fill_price=0.5, paper=True)

    assert ledger.get("m1", paper=True) is None
    assert ledger.get("m2", paper=True) is not None
    assert ledger.paper_balance() == start - 5.0   # debited once, for m2 only


def test_failed_sell_leaves_the_position_intact(ledger, break_on, fresh_db):
    """A sell that fails partway must not shrink the position."""
    ledger.record_buy(make_trade(market_id="m1"), spent_usdc=5.0, shares=10.0,
                      fill_price=0.5, paper=True)

    break_on("INSERT INTO daily_stats")
    with pytest.raises(RuntimeError):
        ledger.record_sell(make_trade(market_id="m1", action="SELL"), shares=4.0,
                           proceeds_usdc=3.0, fill_price=0.75, paper=True)

    ledger.record_buy(make_trade(market_id="m2", trade_id="tx2"), spent_usdc=5.0,
                      shares=10.0, fill_price=0.5, paper=True)

    pos = ledger.get("m1", paper=True)
    assert pos is not None and pos.shares == 10.0     # untouched
    assert ledger.today_pnl_usdc(paper=True) == 0.0   # no P&L booked


def test_successful_writes_still_commit(ledger):
    """The guard must not swallow the happy path."""
    start = ledger.paper_balance()
    ledger.record_buy(make_trade(market_id="m1"), spent_usdc=5.0, shares=10.0,
                      fill_price=0.5, paper=True)

    assert ledger.get("m1", paper=True).shares == 10.0
    assert ledger.paper_balance() == start - 5.0
