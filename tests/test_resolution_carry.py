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


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def market(**overrides) -> dict:
    """A Gamma row that clears every gate: a soccer match already underway,
    0.97 ask, 12 hours from settling."""
    row = {
        "gameStartTime": _iso(NOW - 3600),
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


def non_game(**overrides) -> dict:
    """Weather, tweet counts, index levels — no kick-off, never "live"."""
    row = market(**overrides)
    row.pop("gameStartTime")
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


def test_require_sports_can_be_switched_off():
    """Kept as a switch so a non-sports arm can run as a control."""
    p = _params(require_sports=False)
    assert screen.category_ok("politics", p)
    assert screen.category_ok("", p)


def test_unlabelled_fails_closed_under_require_sports():
    assert not screen.category_ok("", _params())


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


def test_a_rejected_signal_does_not_come_back_every_poll(ledger):
    """The runner alerts before it gates, so a market that fails the slippage
    check leaves no position and would otherwise re-signal every cycle."""
    rows = [market(_end_ts=end_ts(0.5))]
    algo = _algo(ledger, rows)

    assert len(list(algo.poll())) == 1      # emitted; assume the runner skips it
    assert list(algo.poll()) == []          # would have been a duplicate alert

    # ...and it is a cooldown, not a permanent ban: the market is still a
    # perfectly good carry once the price has had time to settle.
    algo._signalled["0xm1"] = time.time() - 1
    assert len(list(algo.poll())) == 1


def test_cooldown_of_zero_disables_it(ledger):
    rows = [market(_end_ts=end_ts(0.5))]
    algo = _algo(ledger, rows, resignal_cooldown_seconds=0.0)
    assert len(list(algo.poll())) == 1
    assert len(list(algo.poll())) == 1


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
        params=_params(require_sports=True),
        market_data=FakeGateway(rows, labels="politics"),
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


def test_caps_must_agree_with_the_bankroll():
    """More positions than the exposure cap can fund only ever produces
    intents that are emitted and then rejected."""
    with pytest.raises(ValueError):
        _params(max_concurrent_positions=20, max_total_exposure_usdc=15.0,
                bet_size_usdc=1.0).validate()
    _params(max_concurrent_positions=15, max_total_exposure_usdc=15.0,
            bet_size_usdc=1.0).validate()


def test_this_algorithm_opts_out_of_signal_alerts():
    """The channel is a trade log: it screens ~2,100 markets a poll."""
    assert _params().notify_signals is False


def test_crypto_is_excluded():
    """A "BTC above X at 4pm" market is a live price, not a decided outcome
    waiting on paperwork — there is nothing to be paid for waiting on."""
    p = _params()
    assert not screen.category_ok("bitcoin,weekly,crypto,crypto prices", p)
    assert screen.category_ok("sports,games,soccer,leagues cup", p)


# ── In-play only ─────────────────────────────────────────────────────────────

def _game(hours_from_now: float, **overrides) -> dict:
    """A market row with a kick-off time relative to NOW."""
    return market(gameStartTime=_iso(NOW + hours_from_now * 3600), **overrides)


def test_a_game_underway_is_tradeable():
    cand = screen.evaluate(_game(-1.0), end_ts(0.2), NOW, _params())
    assert isinstance(cand, screen.Candidate)


def test_pregame_is_rejected():
    """0.95 before kickoff is a forecast; 0.95 with the game underway is a
    scoreboard. Only the second is an outcome waiting on paperwork."""
    assert screen.evaluate(_game(2.0), end_ts(0.5), NOW, _params()) == "pregame"


def test_a_market_that_is_not_a_game_is_rejected():
    """Weather and tweet counts never go live — they just expire."""
    assert screen.evaluate(non_game(), end_ts(0.5), NOW, _params()) == "not a live event"


def test_start_date_is_not_used_as_a_kickoff_fallback():
    """Every market has a startDate; falling back to it would silently
    report everything as already started and disable the gate."""
    row = non_game(startDate="2020-01-01T00:00:00Z")
    assert screen.game_start_ts(row) is None
    assert screen.evaluate(row, end_ts(0.5), NOW, _params()) == "not a live event"


def test_in_play_gate_can_be_switched_off():
    p = _params(require_in_play=False)
    assert isinstance(screen.evaluate(non_game(), end_ts(0.5), NOW, p), screen.Candidate)


def test_esports_is_excluded():
    p = _params()
    assert not screen.category_ok("esports,counter strike 2,games,sports", p)
    assert screen.category_ok("sports,games,soccer,efl championship", p)


def test_the_worst_admissible_fill_stays_above_95c():
    """min_ask exists to floor what we own, not just what we signalled."""
    p = _params()
    assert p.min_ask * (1 - p.max_slippage) > 0.95


def test_spread_cap_admits_in_play_books_but_not_absent_ones():
    """In-play books run wider than pregame; a book quoted 0.97/0.79 is not
    a wide price, it is two lonely orders and no price at all."""
    p = _params()
    assert isinstance(screen.evaluate(_game(-1, bestAsk="0.970", bestBid="0.925"),
                                      end_ts(0.2), NOW, p), screen.Candidate)
    assert screen.evaluate(_game(-1, bestAsk="0.970", bestBid="0.790"),
                           end_ts(0.2), NOW, p) == "spread"


# ── The grace window: trading after the whistle ──────────────────────────────

def test_a_market_that_just_ended_is_tradeable():
    """The purest form of the trade — result known, only the oracle pending.
    Measured post-end books stay live for 10-40 minutes at 0.95-0.99."""
    cand = screen.evaluate(_game(-2), end_ts(-10 / 1440), NOW, _params())
    assert isinstance(cand, screen.Candidate)
    assert cand.days < 0                       # horizon is behind us


def test_a_fossil_is_still_rejected():
    """106 of 500 rows had end dates months past and were never resolved.
    Their quotes are artifacts, not prices."""
    assert screen.evaluate(_game(-800), end_ts(-30), NOW, _params()) == "long past end"


def test_grace_of_zero_restores_the_old_behaviour():
    p = _params(max_hours_past_end=0.0)
    assert screen.evaluate(_game(-2), end_ts(-10 / 1440), NOW, p) == "long past end"


def test_the_settle_soon_floor_does_not_apply_after_the_whistle():
    """A floor on time-to-settle is meaningless once the event is over."""
    p = _params(min_hours_to_resolution=6.0)
    assert isinstance(screen.evaluate(_game(-2), end_ts(-10 / 1440), NOW, p),
                      screen.Candidate)
    assert screen.evaluate(_game(-2), end_ts(0.1), NOW, p) == "resolves too soon"


def test_post_whistle_entries_are_labelled_for_attribution(ledger):
    rows = [market(_end_ts=end_ts(-10 / 1440),
                   gameStartTime=_iso(NOW - 7200))]
    intents = list(_algo(ledger, rows).poll())
    assert len(intents) == 1
    assert intents[0].features["hours_past_end"] == pytest.approx(0.167, abs=0.02)
    assert "ended" in intents[0].reason


# ── Moneyline only ───────────────────────────────────────────────────────────

def test_a_moneyline_is_tradeable():
    row = _game(-1, question="Will Wolverhampton Wanderers FC win on 2026-08-14?")
    assert isinstance(screen.evaluate(row, end_ts(0.2), NOW, _params()), screen.Candidate)


@pytest.mark.parametrize("question, outcomes", [
    # Totals quote Over/Under, not Yes/No.
    ("Wolverhampton Wanderers FC vs. Blackburn Rovers FC: O/U 2.5", '["Over", "No"]'),
    # These are Yes/No but are props, not "who wins" — 682 of 751 live Yes/No
    # sports markets look like this.
    ("Will Galatasaray SK vs. Çorum FK end in a draw?", '["Yes", "No"]'),
    ("Galatasaray SK vs. Çorum FK: Both Teams to Score", '["Yes", "No"]'),
    ("Exact Score: Wolverhampton 3 - 0 Blackburn", '["Yes", "No"]'),
    ("Will Elon Musk post 40-64 tweets from August 13 to August 15?", '["Yes", "No"]'),
])
def test_everything_that_is_not_a_moneyline_is_rejected(question, outcomes):
    row = _game(-1, question=question, outcomes=outcomes)
    assert screen.evaluate(row, end_ts(0.2), NOW, _params()) == "not a winner market"


def test_an_esports_game_winner_does_not_slip_through_on_the_word_winner():
    """`\\bwin\\b` must not match the "Winner" in "Game 2 Winner", and those
    markets quote team names rather than Yes/No anyway."""
    row = _game(-1, question="LoL: T1 Academy vs Dplus KIA - Game 2 Winner",
                outcomes='["T1 Academy", "Dplus KIA"]')
    assert not screen.is_winner_market(row)


def test_sports_is_required_again():
    p = _params()
    assert screen.category_ok("sports,games,soccer,efl championship", p)
    assert not screen.category_ok("weather,hong kong", p)
    assert not screen.category_ok("commodities,gold", p)


# ── Scanning is paged, not accumulated ───────────────────────────────────────

class PagedGateway(FakeGateway):
    """Serves 100-row pages and records what was asked for."""

    def __init__(self, pages, labels="sports,nfl"):
        super().__init__([r for p in pages for r in p], labels)
        self._pages = pages
        self.offsets: list[int] = []

    def top_markets(self, closed=False, limit=100, offset=0,
                    end_date_min=None, end_date_max=None):
        self.offsets.append(offset)
        idx = offset // 100
        return self._pages[idx] if idx < len(self._pages) else []


def _full_page(prefix):
    """100 rows that all fail an early gate — cheap filler."""
    return [market(conditionId=f"{prefix}{i}", bestAsk="0.10") for i in range(100)]


def test_every_page_is_screened_and_a_short_page_ends_the_scan(ledger):
    from algorithms.resolution_carry import ResolutionCarryAlgorithm

    winner = market(conditionId="0xwin", _end_ts=end_ts(0.2),
                    question="Will Team A win on 2026-08-14?")
    gw = PagedGateway([_full_page("a"), _full_page("b"), [winner]])
    algo = ResolutionCarryAlgorithm(params=_params(discovery_pages=21), market_data=gw)
    algo.setup(ledger)

    intents = list(algo.poll())
    assert gw.offsets == [0, 100, 200]      # stopped on the short third page
    assert [i.market_id for i in intents] == ["0xwin"]


def test_scan_is_lazy(ledger):
    """A generator, so a page is fetched only as the screen reaches it —
    that is what keeps one page in memory instead of all 2,100 rows."""
    from algorithms.resolution_carry import ResolutionCarryAlgorithm

    gw = PagedGateway([_full_page("a"), [market(conditionId="0xz")]])
    algo = ResolutionCarryAlgorithm(params=_params(discovery_pages=21), market_data=gw)
    algo.setup(ledger)

    pages = algo._scan_pages()
    assert gw.offsets == []                 # nothing fetched yet
    next(pages)
    assert gw.offsets == [0]                # exactly one page, on demand


# ── The underdog arm ─────────────────────────────────────────────────────────

def test_the_underdog_ask_is_one_minus_the_bid_not_one_minus_the_ask():
    """The spread is paid on whichever side you take. At a 0.97/0.96 book the
    underdog costs 0.04, not 0.03 — a third of the theoretical edge."""
    cand = screen.evaluate(_game(-1), end_ts(0.2), NOW, _params())
    assert cand.ask == 0.97 and cand.bid == 0.96
    assert cand.under_ask == 0.04
    assert cand.under_asset_id == "tok_no"
    assert cand.under_outcome == "No"


def test_underdog_arm_buys_the_other_token(ledger):
    rows = [market(_end_ts=end_ts(0.2), gameStartTime=_iso(NOW - 3600),
                   question="Will Team A win on 2026-08-16?")]
    control = list(_algo(ledger, rows).poll())
    assert control[0].asset_id == "tok_yes"
    assert control[0].signal_price == 0.97
    assert control[0].features["side"] == "favourite"

    treatment = list(_algo(ledger, rows, buy_underdog=True).poll())
    assert treatment[0].asset_id == "tok_no"
    assert treatment[0].outcome == "No"
    assert treatment[0].signal_price == 0.04
    assert treatment[0].features["side"] == "underdog"


def test_both_arms_select_the_identical_markets(ledger):
    """Every gate and the ranking run on the favourite regardless of side, so
    the A/B differs only in the token bought."""
    rows = [
        market(conditionId="0xa", _end_ts=end_ts(0.2), bestAsk="0.96", bestBid="0.95",
               gameStartTime=_iso(NOW - 3600), question="Will A win on 2026-08-16?",
               events=[{"id": "e1"}], clobTokenIds='["a_yes", "a_no"]'),
        market(conditionId="0xb", _end_ts=end_ts(0.2), bestAsk="0.98", bestBid="0.97",
               gameStartTime=_iso(NOW - 3600), question="Will B win on 2026-08-16?",
               events=[{"id": "e2"}], clobTokenIds='["b_yes", "b_no"]'),
    ]
    control = [i.market_id for i in _algo(ledger, rows).poll()]
    treatment = [i.market_id for i in _algo(ledger, rows, buy_underdog=True).poll()]
    assert control == treatment          # same markets, same order


def test_the_variant_wires_the_underdog_arm():
    """A typo'd knob in VARIANTS must crash, not silently run the control."""
    from algorithms.resolution_carry.params import VARIANTS, ResolutionCarryParams

    p = ResolutionCarryParams(name="resolution_carry_underdog_paper")
    assert p.buy_underdog is True
    # A relative slippage gate of 0.005 on a 0.04 entry is a fifth of a cent;
    # the variant restores roughly the control's absolute tolerance.
    assert p.max_slippage * 0.04 == pytest.approx(0.005 * 0.97, abs=0.002)
    assert ResolutionCarryParams(name="resolution_carry_paper").buy_underdog is False
    assert "resolution_carry_underdog_paper" in VARIANTS
