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
    # 0.955, not 0.95, and the extra half-cent is arithmetic rather than
    # taste: the slippage gate admits a fill up to max_slippage *below* the
    # signal, so a 0.950 signal could fill at 0.9453 — and one already did
    # (KÍ vs Lech Poznań, filled 0.949). The floor is meant to be a floor on
    # what we actually own, so it has to sit at 0.95 / (1 - max_slippage) for
    # the worst admissible fill to still land above 0.95.
    min_ask: float = _doc(0.955, "Below this we are forecasting, not carrying. Set so the worst fill the slippage gate allows still lands above 0.95.")
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
    max_ask_spread: float = _doc(0.05, "bestAsk - bestBid ceiling. Not a cost (we hold to resolution) but a limit on how far above the mid we will pay.")

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
