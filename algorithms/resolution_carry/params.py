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
    # 20, i.e. inert, because the primary Gamma label of every sports market
    # is "sports" — at 5 this silently capped the strategy at five positions
    # and made max_concurrent_positions decorative. The cap was written for
    # "twenty political markets that are really one election"; within sports
    # it does not do that job, since two games are not a shared theme and the
    # per-event cap already blocks two legs of the same one. Restore a real
    # value if the non-sports arm ever runs.
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
    # Sports-primary: objective resolution, genuinely independent events, and
    # short horizons — the same 2% is worth ~250x more at a one-day horizon
    # than a two-month one. A switch, not a hard-coding, so the non-sports arm
    # can run as a control.
    require_sports: bool = _doc(True, "Only trade markets Gamma labels sports/esports.")
    exclude_categories: tuple = _doc((), "Gamma category/tag substrings to reject. Deliberately empty — efficiency is the product here.")
    # 60, not 300: measured p95 drift on a market already at 0.95+ is 0.0185
    # over five minutes, roughly the entire return of a 0.98 entry.
    poll_interval_seconds: int = _doc(60, "Seconds between discovery scans. Set by the staleness measurement, not taste.")
    # 21 is everything Gamma will serve: it 422s past offset 2100. The scan
    # also stops early on a short page, so this is a ceiling, not a cost — a
    # one-day window is ~400 rows and exhausts in about four pages. Scanning
    # less is not an option: Gamma orders by volume and the markets this
    # strategy wants — sports games near their end — are the low-volume tail,
    # so a partial scan systematically misses the universe.
    discovery_pages: int = _doc(21, "Max Gamma /markets pages to scan per poll (100 rows each). 21 is Gamma's own offset ceiling.")
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
