"""
Resolution-carry parameter schema.

Pure schema — names, types, docs, and boot-time validation. Values live only
here; a profile block declares what runs, never how it is tuned.
"""

from dataclasses import dataclass, field, fields

from bot.domain.mode import Mode


def _doc(default, doc: str):
    return field(default=default, metadata={"doc": doc})


# Named variants — the one place a knob may differ between two blocks of this
# type, keyed by the `name` a profile declares. Values still live in this file,
# so there is still exactly one place to look up what a knob is set to.
#
# The A/B: identical screen, identical ranking, identical markets — only the
# side traded differs. The control buys the favourite at ~0.97 and needs to be
# right 97% of the time; the treatment buys the same market's underdog at
# ~0.04 and needs 4%. Over the first 33 settlements the control went 30-3 for
# -$2.15 while the mirror of those same trades would have made +$40.05, on a
# 9.1% hit rate against a 4.1% break-even. That is 3 events and proves
# nothing: the 95% interval on 9.1% is [3.1%, 23.6%], which contains
# break-even, and a fair market throws 3+ winners 15% of the time. ~200
# settlements separate the two.
VARIANTS: dict[str, dict] = {
    "resolution_carry_underdog_paper": {
        "buy_underdog": True,
        # Both of these follow from the side flip rather than being tuned.
        #
        # max_slippage is a *relative* gate, and 0.005 of a 0.04 entry is a
        # fifth of a cent — tighter than the CLOB quotes. 0.12 of 0.04 is the
        # same ~0.5c of absolute tolerance the control gets at 0.97.
        "max_slippage": 0.12,
        # The daily loss limit is meant to catch a bad day, not a normal one.
        # At a ~9% hit rate roughly nine trades in ten lose their full dollar,
        # so $5 would suspend this arm within its first hour every day and it
        # would never accumulate a sample. $20 is the whole bankroll — a real
        # stop, not a throttle.
        "daily_loss_limit_usdc": 20.0,
        # Pinned to the original band. The control's floor moved to 0.980 on
        # the measurement above, which would silently move this arm from
        # buying 0.04-0.06 underdogs to buying 0.01-0.02 ones — a different
        # experiment from the one that was started, and a worse one. Holding
        # the band here keeps the arms comparable to their own history.
        #
        # Note the measurement has largely answered this arm's question
        # already: across n=1529 in the old band the favourite failed 3.01% of
        # the time against a 3.15% break-even, so the underdog mirror is a
        # ~break-even-to-negative trade, not the 9.1% hit rate the first 33
        # settlements suggested. Retire it once the sample agrees.
        "min_ask": 0.955,
        "max_ask": 0.985,
        "max_ask_spread": 0.05,
    },
    # The execution A/B. Identical screen, identical band, identical markets —
    # the only difference is whether we cross the spread or rest at the bid.
    #
    # The control's +0.84% is measured net of taker costs, which at these
    # prices are a 0.08% fee plus a 0.25% half-spread. A maker pays neither,
    # so if it fills at the same rate this arm should earn roughly a third of
    # a point more per trade. It will not fill at the same rate, and by how
    # much less is the number this arm exists to produce: paper cannot answer
    # it by assumption, so the order actually has to rest and be counted.
    #
    # Read it off `signals.skip_reason`: "maker: resting" is posted,
    # "maker: unfilled" expired, and an executed row is a fill. Fill rate is
    # fills / (fills + unfilled), and it is the whole result.
    "resolution_carry_maker_paper": {
        "entry_style": "maker",
        # The band is open about a minute, so an order still resting after
        # five is quoting a market that has moved on.
        "maker_ttl_seconds": 300.0,
    },
}


@dataclass(frozen=True)
class ResolutionCarryParams:
    name: str = "resolution_carry"
    mode: Mode = Mode.PAPER          # fail-safe; the profile block sets it

    # ── Price band ───────────────────────────────────────────────────────────
    # Measured, not assumed. 17,557 closed moneylines (every one in a 25-day
    # window, so no selection on who traded them), one entry per market at its
    # first in-play touch of each ask band, net of a 1c spread and the
    # 5%*p*(1-p) taker fee:
    #
    #   ask band       n    failures   rate    break-even    net ROI
    #   0.950-0.960   623      35      5.62%     4.73%       -1.17%
    #   0.960-0.970   559      20      3.58%     3.69%       -0.06%
    #   0.970-0.980   551      16      2.90%     2.63%       -0.42%
    #   0.980-0.990   627       4      0.64%     1.54%       +0.84%
    #   0.990-0.995   483       4      0.83%     0.80%       -0.07%
    #
    # The old band (0.955-0.985) priced to -0.01% over n=1529 — a coin flip
    # that pays its own costs, which is exactly the live 30-3 / -$2.15 record.
    # The edge is entirely above 0.975: at 0.975-0.990 the failure rate is
    # 0.82% against a 1.78% break-even, and the 95% upper bound (1.68%) sits
    # below break-even, so it is established rather than merely favourable.
    #
    # The floor is 0.980 rather than 0.975 for the same arithmetic as before:
    # the slippage gate admits a fill up to max_slippage *below* the signal,
    # and 0.980 * (1 - 0.005) = 0.975 keeps the worst admissible fill inside
    # the proven band. Below 0.975 the trade is measurably negative.
    min_ask: float = _doc(0.980, "Below this the failure rate exceeds the residual. Set so the worst fill the slippage gate allows still lands at/above 0.975.")
    max_ask: float = _doc(0.990, "Above this the residual cannot cover the tail (0.990-0.995 measured -0.07%).")

    # ── Time value ───────────────────────────────────────────────────────────
    # Time is the binding constraint, not price: 2% is excellent over 30 days
    # and worse than cash over 180. Unknown end date fails closed.
    max_days_to_resolution: float = _doc(1.0, "Capital lockup ceiling, in days.")
    # Inert at a one-day window: annualisation floors the horizon at a day, so
    # the weakest trade the band allows (0.985) still reports ~560%/yr. Kept
    # because it becomes the binding gate the moment the window widens.
    min_annualized_return: float = _doc(0.25, "Win-case return annualized over the wait must beat this (0.25 = +25%/yr).")
    # 0, not 6: a floor in hours is a floor on the whole thesis. The trade
    # this strategy exists to take is a game decided on the pitch and sitting
    # at 0.97 while it waits to settle, which is minutes from resolution, not
    # hours. A 6h floor excluded exactly that and left a universe of one
    # sports market (measured across all 2,100 Gamma will serve).
    #
    # At 0 the gate still rejects a market whose end date has *passed* —
    # trading has effectively stopped, there is no carry left to earn, and
    # the quoted price is a dead book rather than an opinion. That is the
    # part worth keeping, so validate() refuses a negative value.
    min_hours_to_resolution: float = _doc(0.0, "Skip markets settling sooner than this. Applies only to markets that have not yet reached their end date.")

    # The grace window: how far past its scheduled end a market may be and
    # still be tradeable. This is where the purest version of the trade lives
    # — the whistle has gone, the result is known, and only the oracle is
    # outstanding — and it was excluded outright until measured.
    #
    # From the archive, restricted to prices recorded after the scheduled end:
    #
    #   band          n   resolved YES   edge     window stays open
    #   0.950-0.980  16      100%       +0.035    median 11m, max 36m
    #   0.980-0.990   9      100%       +0.015    median 10m, max 24m
    #   0.990-0.995   9      100%       +0.008    median  4m, p90 14h
    #   0.995-1.000  40      100%       +0.002    median 42m, max 30h
    #
    # 6h has an order of magnitude of margin at both ends: the tradeable
    # window at 0.95-0.99 closes within 36 minutes, and the fossils this must
    # keep excluding are months old. Note the n: 0 failures in 16 still admits
    # a true failure rate near 19%, against a 3.5% break-even. This window is
    # here to *collect* the evidence, not because the evidence is in.
    max_hours_past_end: float = _doc(6.0, "How many hours past its scheduled end a market may be and still trade. 0 rejects everything past its end date.")

    # ── Liquidity ────────────────────────────────────────────────────────────
    min_market_liquidity_usdc: float = _doc(5_000.0, "Gamma `liquidityClob` floor — a depth proxy, not a measurement.")
    # 0.05, loosened from 0.02 once require_in_play made 0.02 unsatisfiable:
    # in-play books are wider than pregame ones, because the price is moving
    # and makers widen to protect themselves. At 0.02 nothing live ever
    # qualified.
    #
    # The spread is not a cost here — we hold to resolution and never cross
    # back over the bid — so this is a judgement about whether the ask can be
    # trusted, not about friction. The thing to watch is how far the ask sits
    # above the mid, because that is the premium being paid over what the
    # book collectively thinks: at a 0.069 spread the ask was 3.4c over mid
    # for a trade returning 2.1c, which is negative expectancy if the mid is
    # nearer the truth. Half of this cap (2.5c) against a 1.5-4.5c return is
    # already the outer edge of defensible.
    # Now a measured constraint rather than a judgement. Re-running the
    # 0.975-0.990 band against wider assumed spreads: +1.14% at 0.005, +0.89%
    # at 0.01, +0.40% at 0.02, and -0.07% at 0.03. The whole edge is one to
    # two cents wide, so a 5c cap admitted precisely the books that erase it.
    # In-play moneylines at 0.98+ with real depth quote a 0.005 median spread
    # (0.009 at p75), so 0.015 rejects the bad books without emptying the
    # funnel — it is roughly the `spread <= 1 - ask` relative gate this
    # comment used to recommend, expressed as the constant the band allows.
    max_ask_spread: float = _doc(0.015, "bestAsk - bestBid ceiling. The edge is 1-2c wide, so a wide book erases it: measured +0.89% at a 1c spread, -0.07% at 3c.")

    # ── Diversification (the load-bearing risk control) ──────────────────────
    # Twenty positions at 0.98 that are all legs of one election are one
    # position at 0.98 with twenty times the size.
    # Held equal to max_total_exposure_usdc / bet_size_usdc on purpose. When
    # this sits above what the exposure cap can fund, every poll emits
    # intents that provably cannot be filled, and each one costs a rejected
    # dispatch. validate() keeps the two in step.
    max_concurrent_positions: int = _doc(15, "Hard cap on simultaneously open markets.")
    max_positions_per_event: int = _doc(1, "Legs per Gamma eventId.")
    # 20, i.e. inert. Set while the arm was sports-only, where it was
    # degenerate: the primary Gamma label of every sports market is "sports",
    # so a cap of 5 silently capped the strategy at five positions and made
    # max_concurrent_positions decorative.
    #
    # With require_sports back on, every primary label is "sports" again, so
    # this stays inert by necessity rather than by choice. The per-event cap
    # is what carries the diversification. Revisit if the universe ever
    # widens beyond sports.
    max_positions_per_category: int = _doc(20, "Positions sharing a primary Gamma category.")

    # ── Sizing ───────────────────────────────────────────────────────────────
    # Flat size: conviction sizing makes no sense when the thesis is "the
    # price is correct".
    bet_size_usdc: float = _doc(1.0, "Flat stake per position (USDC).")
    max_position_size_usdc: float = _doc(1.0, "Max spend per market — flat sizing, so this equals bet_size_usdc.")
    max_total_exposure_usdc: float = _doc(15.0, "Max total open exposure for this algorithm.")
    daily_loss_limit_usdc: float = _doc(5.0, "Suspend buys if realized P&L is down this much today.")
    min_order_size_usdc: float = _doc(1.0, "Skip orders smaller than this.")

    # ── Execution ────────────────────────────────────────────────────────────
    # At 2% gross, one tick of slippage is a quarter of the return: this
    # strategy posts a limit and accepts non-fills rather than paying up.
    order_type: str = _doc("limit", "'limit' (GTC) or 'market' (FOK/FAK). Limit is deliberate here.")

    # 'taker' is the measured configuration: the +0.84% in the band above is
    # net of crossing the spread and paying theta*p*(1-p). Note `order_type =
    # "limit"` does NOT make us a maker — a GTC posted at the ask crosses and
    # takes. Only entry_style does, by resting at the bid under post_only.
    #
    # Maker is strictly cheaper (no fee, no half-spread — together ~0.33% at
    # these prices) and strictly less certain: you fill only when someone
    # sells to you, which selects for the moments the price is falling. That
    # adverse selection is unmeasurable offline, which is why this is an arm
    # rather than a default.
    entry_style: str = _doc("taker", "'taker' crosses the spread; 'maker' rests at the bid under post_only and pays no fee.")
    maker_ttl_seconds: float = _doc(300.0, "Cancel a resting maker order after this long. The band is open ~1 minute, so a stale order is a bet on a market that has moved on.")
    max_slippage: float = _doc(0.005, "Max drift from the signal ask before we skip. Half a cent is a quarter of the return.")

    # ── Screening + cadence ──────────────────────────────────────────────────
    # Back on, and this time for the reason sports actually earns rather than
    # the one it was first given. It is not about resolution *timing* — that
    # was measured and sports is the less punctual class (92% against
    # essentially 100% for everything else). It is that a scoreline and a
    # clock are the most legible source of certainty available: at 95% with
    # the game underway, the market is reading a state of the world, not
    # forecasting one. Weather, tweet counts and index levels are still
    # forecasts at 95%, however punctual their expiry.
    require_sports: bool = _doc(True, "Only trade markets Gamma labels sports/esports.")
    # Crypto is out entirely. A "will BTC be above X at 4pm" market is not a
    # question awaiting settlement — it is a live price that keeps moving
    # until the instant it expires, so there is no decided outcome to be paid
    # for waiting on, and the ask reprices continuously against us. The rest
    # of the universe is markets whose answer is already determined and only
    # the paperwork is pending, which is the entire thesis.
    #
    # A substring on the Gamma label is enough, and that is measured rather
    # than assumed: of 130 crypto markets in a one-day window, 130 carried a
    # "crypto" label, and 0 of 300 sampled markets carried no labels at all
    # (the case this screen would fail open on).
    # Esports joins crypto. A best-of-three at 0.95 is one teamfight from
    # 0.40 — the tail arrives far more often than the price implies, which is
    # exactly the miscalibration this strategy cannot survive. Also measured:
    # 94 of 94 esports markets carry an "esports" label, and the 66 further
    # markets that carry it without an esports-looking title are all handicap
    # legs of the same matches, so the substring catches them too.
    exclude_categories: tuple = _doc(("crypto", "esports"), "Gamma category/tag substrings to reject.")

    # Only markets whose game has actually kicked off. Gamma ships
    # `gameStartTime` on 1,871 of 2,100 markets in a one-day window; the 229
    # without it are not games at all (weather, tweet counts, index levels)
    # and are rejected, since a thing that merely expires is never live.
    require_in_play: bool = _doc(True, "Only trade markets whose gameStartTime has passed. Markets with no gameStartTime are rejected.")

    # Moneyline only — "Will <team> win on <date>?" — on the hypothesis that
    # 95% on who-wins is a steadier 95% than 95% on anything else, because it
    # is read off a scoreline and a clock rather than forecast. An in-play
    # O/U 0.5 at 95% is still a prediction that a goal will arrive; a prop
    # can turn on a single incident.
    #
    # Two conditions are needed, because either alone leaks. Yes/No outcomes
    # exclude totals (Over/Under) and esports head-to-heads (team names), but
    # 682 of 751 live Yes/No sports markets are props — draws, both-teams-to-
    # score, exact scorelines, tweet counts. Requiring "win" as a whole word
    # cuts those and leaves exactly one shape.
    require_winner_market: bool = _doc(True, "Only trade moneyline markets: Yes/No outcomes whose question contains 'win'.")

    # Buy the other side of every market this screen picks. The screen and the
    # ranking still run on the favourite, so both arms select exactly the same
    # markets and differ only in the token taken — a controlled mirror rather
    # than a second strategy.
    #
    # Note this inverts the thesis, and that is the point of testing it. The
    # control asserts the price is *correct* and collects a fee for waiting.
    # This asserts the price is *wrong* at the tail — a forecasting claim, of
    # the kind that sank copy_trade. It is here because the control's own
    # first 33 settlements suggested it, not because the argument is good.
    buy_underdog: bool = _doc(False, "Buy the opposite side of each selected market at its own ask.")
    # 15, from the archive: across 4,765 transits through this band on tokens
    # that finished at/above 0.99, 99% lasted a single one-minute sample. The
    # band is open for about a minute, so a 60s poll lands inside it roughly
    # once and misses outright whenever the transit falls between two polls.
    # A scan is 21 pages / ~6.5s, so the effective cycle is ~21s and a poll
    # never overlaps its own scan. Going much below 15s buys little: the
    # scan time, not the sleep, is most of the cycle.
    poll_interval_seconds: int = _doc(15, "Seconds between discovery scans. Set by the band-dwell measurement, not taste.")
    # 21 is everything Gamma will serve: it 422s past offset 2100. Note that
    # even a one-day window fills all 21 pages — there are more than 2,100
    # markets ending within a day, so we see the highest-volume 2,100 of them
    # and never the tail. That is survivable only because
    # min_market_liquidity_usdc would reject most of that tail anyway; it is
    # the reason to be suspicious of any claim that this scan is exhaustive.
    discovery_pages: int = _doc(21, "Max Gamma /markets pages to scan per poll (100 rows each). 21 is Gamma's own offset ceiling.")
    # A filled signal becomes a position and is deduped by that forever; a
    # *rejected* one leaves nothing behind, so without a cooldown it returns
    # every poll. Dispatch alerts before it gates, so a parked market failing
    # the slippage check would post to Discord every ~21s for hours. At 300s
    # that is a retry every five minutes instead. Costs nothing on a real
    # transit, which is out of the band inside a minute either way. 0 disables.
    resignal_cooldown_seconds: float = _doc(300.0, "Hold a market back from re-signalling for this long after an intent is emitted. 0 disables.")
    settle_check_every: int = _doc(12, "Polls between market-resolution sweeps.")

    # ── Notifications ────────────────────────────────────────────────────────
    # Off, unlike every sibling. This strategy screens ~2,100 markets a poll
    # and emits far more candidates than it fills, so signal and risk-block
    # alerts would bury the ones that matter. The channel is a trade log:
    # fills, settlements, and failures only — what happened, not what was
    # considered. The `signals` table still records every candidate.
    notify_signals: bool = _doc(False, "Post SIGNAL and risk-block alerts before execution. Off = fills and settlements only.")
    webhook_url: str = _doc(
        "https://discord.com/api/webhooks/1537542638591410239/Cm8imiHekuEMjqj_EXRfamB61RkmHazqIQRVXqTTzK_fvdN7ScijyupxDONtTDQTqgAQ",
        "Discord webhook for this algorithm's trade alerts. Required — there is no global fallback.",
    )

    # ── Paper ────────────────────────────────────────────────────────────────
    paper_starting_balance: float = _doc(20.0, "Virtual balance — seeded into the DB on FIRST run only.")
    paper_fee_bps: float = _doc(0.0, "Modeled paper fee, basis points.")

    def __post_init__(self) -> None:
        """Apply this name's variant overrides, if it has any."""
        known = {f.name for f in fields(self)}
        for knob, value in VARIANTS.get(self.name, {}).items():
            # Same fail-fast contract as the profile loader: a typo'd knob is
            # a crash, never a silent no-op that quietly runs the control.
            if knob not in known:
                raise ValueError(
                    f"VARIANTS['{self.name}'] sets unknown knob '{knob}'."
                )
            object.__setattr__(self, knob, value)

    def validate(self) -> None:
        """Boot-time sanity checks — called by the profile loader."""
        if not (0.0 < self.min_ask < self.max_ask < 1.0):
            raise ValueError(
                f"Need 0 < min_ask < max_ask < 1, got {self.min_ask}/{self.max_ask}."
            )
        if self.order_type not in ("market", "limit"):
            raise ValueError(f"order_type must be 'market' or 'limit', got '{self.order_type}'.")
        if self.entry_style not in ("taker", "maker"):
            raise ValueError(
                f"entry_style must be 'taker' or 'maker', got '{self.entry_style}'."
            )
        if self.entry_style == "maker":
            if self.order_type != "limit":
                raise ValueError(
                    "entry_style='maker' needs order_type='limit' — a market "
                    "order crosses the book by definition and can only take."
                )
            if self.maker_ttl_seconds <= 0:
                raise ValueError(
                    "maker_ttl_seconds must be > 0 — an order with no expiry "
                    "rests forever on a market that has already resolved."
                )
        if self.bet_size_usdc > self.max_position_size_usdc:
            raise ValueError(
                f"bet_size_usdc ${self.bet_size_usdc} exceeds max_position_size_usdc "
                f"${self.max_position_size_usdc} — every buy would fail the risk check."
            )
        fundable = int(self.max_total_exposure_usdc // max(self.bet_size_usdc, 1e-9))
        if self.max_concurrent_positions > fundable:
            raise ValueError(
                f"max_concurrent_positions ({self.max_concurrent_positions}) exceeds "
                f"the {fundable} positions max_total_exposure_usdc "
                f"(${self.max_total_exposure_usdc}) can fund at ${self.bet_size_usdc} "
                f"each — the excess would only ever be emitted and rejected."
            )
        if self.min_hours_to_resolution < 0:
            raise ValueError(
                "min_hours_to_resolution must be >= 0 — a negative floor buys "
                "markets whose end date has passed, where the quoted price is "
                "a dead book and there is no carry left to earn."
            )
        if self.max_positions_per_event < 1 or self.max_positions_per_category < 1:
            raise ValueError("Per-event and per-category caps must be at least 1.")
        if not self.webhook_url:
            raise ValueError(
                "webhook_url is required — there is no global fallback, so an "
                "algorithm without one would trade without ever notifying."
            )
