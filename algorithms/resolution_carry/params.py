"""
Resolution-carry parameter schema.

Pure schema — names, types, docs, and boot-time validation. Values live only
here; a profile block declares what runs, never how it is tuned.
"""

from dataclasses import dataclass, field

from bot.domain.mode import Mode


def _doc(default, doc: str):
    return field(default=default, metadata={"doc": doc})


@dataclass(frozen=True)
class ResolutionCarryParams:
    name: str = "resolution_carry"
    mode: Mode = Mode.PAPER          # fail-safe; the profile block sets it

    # ── Price band ───────────────────────────────────────────────────────────
    min_ask: float = _doc(0.95, "Below this we are forecasting, not carrying.")
    max_ask: float = _doc(0.985, "Above this the residual cannot cover the tail.")

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
    min_hours_to_resolution: float = _doc(0.0, "Skip markets settling sooner than this. 0 still rejects markets already past their end date.")

    # ── Liquidity ────────────────────────────────────────────────────────────
    min_market_liquidity_usdc: float = _doc(5_000.0, "Gamma `liquidityClob` floor — a depth proxy, not a measurement.")
    max_ask_spread: float = _doc(0.02, "bestAsk - bestBid ceiling; a wide book means there is no real price.")

    # ── Diversification (the load-bearing risk control) ──────────────────────
    # Twenty positions at 0.98 that are all legs of one election are one
    # position at 0.98 with twenty times the size.
    max_concurrent_positions: int = _doc(20, "Hard cap on simultaneously open markets.")
    max_positions_per_event: int = _doc(1, "Legs per Gamma eventId.")
    # 20, i.e. inert. Set while the arm was sports-only, where it was
    # degenerate: the primary Gamma label of every sports market is "sports",
    # so a cap of 5 silently capped the strategy at five positions and made
    # max_concurrent_positions decorative.
    #
    # Now that require_sports is off, labels do vary and a real value would
    # do its intended job again — capping "twenty political markets that are
    # really one election" — at the price of capping sports, which is where
    # the flow is. Left inert deliberately; the per-event cap carries the
    # diversification. Revisit once paper shows how loss concentrates.
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
    max_slippage: float = _doc(0.005, "Max drift from the signal ask before we skip. Half a cent is a quarter of the return.")

    # ── Screening + cadence ──────────────────────────────────────────────────
    # Off, because the thing sports was standing in for turned out to be
    # measurable directly. The worry was that only a game has a knowably
    # certain resolution time; but Gamma's end date is what the horizon gate
    # already reads, and it holds. Over a 6-hour slice of end dates three
    # days back, non-sports markets honoured their stated date 983 of 984
    # times, against 1117 of 1215 for sports — sports is the *less* punctual
    # class, because games get postponed. The day cap does the work the sports
    # screen was hired for. `market_category` is logged on every signal, so
    # the arms can still be compared on realized P&L.
    require_sports: bool = _doc(False, "Only trade markets Gamma labels sports/esports.")
    exclude_categories: tuple = _doc((), "Gamma category/tag substrings to reject. Deliberately empty — efficiency is the product here.")
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
    webhook_url: str = _doc(
        "https://discord.com/api/webhooks/1537542638591410239/Cm8imiHekuEMjqj_EXRfamB61RkmHazqIQRVXqTTzK_fvdN7ScijyupxDONtTDQTqgAQ",
        "Discord webhook for this algorithm's trade alerts. Required — there is no global fallback.",
    )

    # ── Paper ────────────────────────────────────────────────────────────────
    paper_starting_balance: float = _doc(20.0, "Virtual balance — seeded into the DB on FIRST run only.")
    paper_fee_bps: float = _doc(0.0, "Modeled paper fee, basis points.")

    def validate(self) -> None:
        """Boot-time sanity checks — called by the profile loader."""
        if not (0.0 < self.min_ask < self.max_ask < 1.0):
            raise ValueError(
                f"Need 0 < min_ask < max_ask < 1, got {self.min_ask}/{self.max_ask}."
            )
        if self.order_type not in ("market", "limit"):
            raise ValueError(f"order_type must be 'market' or 'limit', got '{self.order_type}'.")
        if self.bet_size_usdc > self.max_position_size_usdc:
            raise ValueError(
                f"bet_size_usdc ${self.bet_size_usdc} exceeds max_position_size_usdc "
                f"${self.max_position_size_usdc} — every buy would fail the risk check."
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
