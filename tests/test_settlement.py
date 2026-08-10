"""
Settlement policy tests — when a position may be booked at a close price.

The rule this file protects: a binary-looking CLOB price is only evidence of
resolution once trading has STOPPED. `fetch_resolution_price` is the last
trade on an open book, and a heavy favourite sits at 0.97 for weeks without
being decided. Settling there books P&L off a live book, drifts the DB from
the on-chain position, and writes a signal outcome label that is write-once.
"""

import pytest

from bot.execution.settlement import resolve_close_price


@pytest.fixture
def stub_gamma(monkeypatch):
    """Pin the Gamma market payload and the CLOB last-trade price."""
    from bot.polymarket import api

    def _set(market, clob_price=None):
        monkeypatch.setattr(api, "fetch_market_resolution", lambda *a, **kw: market)
        monkeypatch.setattr(api, "fetch_resolution_price", lambda *a, **kw: clob_price)
    return _set


# ── The bug this file exists for ────────────────────────────────────────────


def test_open_market_at_high_price_does_not_settle(stub_gamma):
    """A live favourite at 0.99 is not a resolution."""
    stub_gamma({"closed": False, "outcomePrices": '["0.99", "0.01"]'}, clob_price=0.99)
    assert resolve_close_price("m1", "winner") is None


def test_open_market_at_low_price_does_not_settle(stub_gamma):
    stub_gamma({"closed": False}, clob_price=0.01)
    assert resolve_close_price("m1", "loser") is None


def test_missing_gamma_data_does_not_settle(stub_gamma):
    """No Gamma payload at all → no corroboration that trading ended."""
    stub_gamma(None, clob_price=0.99)
    assert resolve_close_price("m1", "winner") is None


# ── The cases that must still work ──────────────────────────────────────────


def test_closed_market_with_binary_clob_settles(stub_gamma):
    """Gamma has closed the market but has no usable outcomePrices; the CLOB
    has settled. This is the Gamma-lag case the fallback exists for."""
    stub_gamma({"closed": True}, clob_price=0.99)
    assert resolve_close_price("m1", "winner") == 1.0

    stub_gamma({"closed": True}, clob_price=0.01)
    assert resolve_close_price("m1", "loser") == 0.0


def test_resolved_gamma_prices_win_over_clob(stub_gamma):
    """When Gamma reports a determined outcome, its prices are canonical."""
    stub_gamma(
        {"resolved": True,
         "clobTokenIds": '["winner", "loser"]',
         "outcomePrices": '["1", "0"]'},
        clob_price=0.60,     # ignored
    )
    assert resolve_close_price("m1", "winner") == 1.0
    assert resolve_close_price("m1", "loser") == 0.0


def test_closed_but_undetermined_mid_price_does_not_settle(stub_gamma):
    """Trading ended, UMA still deciding, book sitting mid-range → wait."""
    stub_gamma({"closed": True, "outcomePrices": '["0.60", "0.40"]'}, clob_price=0.60)
    assert resolve_close_price("m1", "winner") is None
