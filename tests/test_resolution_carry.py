"""
Resolution-carry tests — the screening funnel and one end-to-end poll.

Only the read boundary is stubbed, and through the gateway rather than by
monkeypatching: the strategy takes a `MarketDataGateway`, so a fake one is
the whole HTTP stub. Ledger, params, and screening run real code.
"""

import time
from collections import Counter

import pytest

from bot.domain.intents import OpenIntent
from bot.domain.mode import Mode

from algorithms.resolution_carry import screen
from algorithms.resolution_carry.params import ResolutionCarryParams


NOW = time.time()


def _params(**overrides):
    base = dict(
        name="resolution_carry_test",
        mode=Mode.PAPER,
        webhook_url="http://hook",
        settle_check_every=1_000_000,     # off unless a test opts in
        discovery_pages=1,
    )
    base.update(overrides)
    return ResolutionCarryParams(**base)


def market(**overrides) -> dict:
    """A Gamma row that clears every gate: sports, 0.97 ask, 12 hours out."""
    row = {
        "conditionId": "0xm1",
        "clobTokenIds": '["tok_yes", "tok_no"]',
        "outcomes": '["Yes", "No"]',
        "question": "Will Team A win?",
        "bestAsk": "0.97",
        "bestBid": "0.96",
        "liquidityClob": "25000",
        "events": [{"id": "ev1"}],
    }
    row.update(overrides)
    return row


def end_ts(days: float) -> float:
    return NOW + days * 86_400


# ── The funnel ───────────────────────────────────────────────────────────────

def test_clean_row_becomes_a_candidate():
    cand = screen.evaluate(market(), end_ts(0.5), NOW, _params())
    assert isinstance(cand, screen.Candidate)
    assert (cand.market_id, cand.asset_id, cand.outcome) == ("0xm1", "tok_yes", "Yes")
    assert cand.event_id == "ev1"
    # (1-0.97)/0.97, annualised against the 1-day floor.
    assert cand.annualized == pytest.approx((0.03 / 0.97) * 365, rel=1e-6)


@pytest.mark.parametrize("row, days, reason", [
    (market(bestAsk="0.99"),  0.5, "out of band"),  # residual too thin
    (market(bestAsk="0.94"),  0.5, "out of band"),  # forecasting, not carrying
    (market(bestBid="0.90"),  0.5, "spread"),       # no real price
    (market(liquidityClob="100"), 0.5, "illiquid"),
    (market(clobTokenIds="[]"),   0.5, "no token id"),
    (market(),               -0.1, "past end date"),
    (market(),                3,   "resolves too far out"),
])
def test_gates_reject_with_a_reason(row, days, reason):
    assert screen.evaluate(row, end_ts(days), NOW, _params()) == reason


def test_unknown_end_date_fails_closed():
    """An un-priceable wait cannot clear a time-value hurdle."""
    assert screen.evaluate(market(), None, NOW, _params()) == "no end date"


def test_a_market_minutes_from_settling_is_tradeable():
    """The whole thesis: a game decided on the pitch, sitting at 0.97 while
    it waits to settle. A floor in hours would exclude exactly this."""
    cand = screen.evaluate(market(), end_ts(10 / 1440), NOW, _params())
    assert isinstance(cand, screen.Candidate)
    # Annualising floors the horizon at a day, so a 10-minute wait is not
    # reported as a four-figure return.
    assert cand.annualized == pytest.approx((0.03 / 0.97) * 365, rel=1e-6)


def test_the_annualized_hurdle_still_bites_on_a_wider_window():
    """Inert at a one-day cap — the weakest trade the band allows still
    reports ~560%/yr — but it is the binding gate as soon as the window
    widens, so keep it covered."""
    p = _params(max_days_to_resolution=45.0)
    assert screen.evaluate(market(bestAsk="0.98", bestBid="0.97"), end_ts(40), NOW, p) \
        == "not worth the wait"


def test_an_hours_floor_still_applies_when_set():
    p = _params(min_hours_to_resolution=6.0)
    assert screen.evaluate(market(), end_ts(0.1), NOW, p) == "resolves too soon"


def test_validate_refuses_a_negative_hours_floor():
    """Negative would buy markets past their end date — a dead book."""
    with pytest.raises(ValueError):
        _params(min_hours_to_resolution=-1.0).validate()


def test_sports_is_required_not_excluded():
    p = _params()
    assert screen.category_ok("sports,nfl", p)
    assert not screen.category_ok("politics", p)
    assert not screen.category_ok("", p)          # unlabelled fails closed
    # ...unless the control arm turns the switch off.
    assert screen.category_ok("politics", _params(require_sports=False))


# ── Diversification ──────────────────────────────────────────────────────────

def _cand(market_id, event, category, annualized) -> screen.Candidate:
    return screen.Candidate(
        market_id=market_id, asset_id=f"tok_{market_id}", question="q",
        outcome="Yes", ask=0.97, bid=0.96, liquidity=25_000.0,
        end_ts=end_ts(0.5), days=0.5, annualized=annualized,
        event_id=event, category=category,
    )


def test_select_ranks_by_annualized_return():
    picks = screen.select(
        [_cand("a", "e1", "sports", 1.0), _cand("b", "e2", "sports", 9.0)],
        Counter(), Counter(), slots=1, p=_params(),
    )
    assert [c.market_id for c in picks] == ["b"]


def test_one_leg_per_event_within_a_single_poll():
    """Twenty legs of one event are one position with twenty times the size."""
    picks = screen.select(
        [_cand("a", "e1", "sports", 9.0), _cand("b", "e1", "sports", 8.0)],
        Counter(), Counter(), slots=5, p=_params(),
    )
    assert [c.market_id for c in picks] == ["a"]


def test_held_positions_consume_the_caps():
    p = _params(max_positions_per_category=2)
    picks = screen.select(
        [_cand("a", "e1", "sports", 9.0), _cand("b", "e2", "sports", 8.0)],
        Counter({"e9": 1}), Counter({"sports": 1}), slots=5, p=p,
    )
    assert [c.market_id for c in picks] == ["a"]


# ── One end-to-end poll ──────────────────────────────────────────────────────

class FakeGateway:
    """The read boundary, stubbed. Same surface the strategy actually calls."""

    def __init__(self, rows, labels="sports,nfl"):
        self._rows = rows
        self._labels = labels

    def top_markets(self, closed=False, limit=100, offset=0,
                    end_date_min=None, end_date_max=None):
        return self._rows if offset == 0 else []

    def market(self, market_id):
        return next((r for r in self._rows if r["conditionId"] == market_id), None)

    def market_labels(self, market):
        return self._labels

    @staticmethod
    def market_end_ts(market):
        return market.get("_end_ts")


def _algo(ledger, rows, **overrides):
    from algorithms.resolution_carry import ResolutionCarryAlgorithm

    a = ResolutionCarryAlgorithm(
        params=_params(**overrides), market_data=FakeGateway(rows),
    )
    a.setup(ledger)
    return a


def test_poll_emits_one_flat_stake_open_intent(ledger):
    rows = [market(_end_ts=end_ts(0.5))]
    intents = list(_algo(ledger, rows).poll())

    assert len(intents) == 1
    intent = intents[0]
    assert isinstance(intent, OpenIntent)
    assert intent.asset_id == "tok_yes"
    assert intent.usdc_amount == 1.0
    assert intent.signal_price == 0.97          # the ask, not the mid
    assert intent.features["market_category"] == "sports"
    assert intent.features["event_id"] == "ev1"


def test_a_held_market_is_never_re_opened(ledger):
    """Being at size is the dedupe — there is no seen-ring to go stale."""
    from tests.conftest import make_trade

    rows = [market(_end_ts=end_ts(0.5))]
    algo = _algo(ledger, rows)
    ledger.record_buy(
        make_trade(market_id="0xm1", asset_id="tok_yes", price=0.97),
        spent_usdc=1.0, shares=1.03, fill_price=0.97, paper=True,
    )
    assert list(algo.poll()) == []


def test_full_book_stops_scanning(ledger):
    from tests.conftest import make_trade

    rows = [market(_end_ts=end_ts(0.5))]
    algo = _algo(ledger, rows, max_concurrent_positions=1)
    ledger.record_buy(
        make_trade(market_id="0xother", asset_id="tok_x", price=0.97),
        spent_usdc=1.0, shares=1.03, fill_price=0.97, paper=True,
    )
    assert list(algo.poll()) == []


def test_non_sports_market_is_screened_out(ledger):
    from algorithms.resolution_carry import ResolutionCarryAlgorithm

    rows = [market(_end_ts=end_ts(0.5))]
    algo = ResolutionCarryAlgorithm(
        params=_params(), market_data=FakeGateway(rows, labels="politics"),
    )
    algo.setup(ledger)
    assert list(algo.poll()) == []


# ── Boot-time validation ─────────────────────────────────────────────────────

@pytest.mark.parametrize("overrides", [
    {"min_ask": 0.99, "max_ask": 0.98},      # inverted band
    {"max_ask": 1.0},                        # a certainty pays nothing
    {"order_type": "gtc"},
    {"bet_size_usdc": 5.0},                  # over max_position_size_usdc
    {"webhook_url": ""},
])
def test_validate_rejects(overrides):
    with pytest.raises(ValueError):
        _params(**overrides).validate()


def test_shipped_defaults_validate():
    ResolutionCarryParams(webhook_url="http://hook").validate()
