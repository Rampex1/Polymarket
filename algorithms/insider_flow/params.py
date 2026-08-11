"""
Insider-flow parameter schema.

Pure schema — names, types, docs. No defaults: every profile block states
every knob.
"""

from dataclasses import dataclass, field

from bot.domain.mode import Mode


def _doc(default, doc: str):
    return field(default=default, metadata={"doc": doc})


# " vs " also matches head-to-head political markets; acceptable because
# sports dominates the big-cash feed by an order of magnitude.
_DEFAULT_EXCLUDES = (" vs. ", " vs ", "O/U", "Spread")


@dataclass(frozen=True)
class InsiderFlowParams:
    name: str = "insider_flow"
    mode: Mode = Mode.PAPER          # fail-safe; the profile block sets it

    # ── Signal filters ───────────────────────────────────────────────────────
    min_cash_size_usdc: float = _doc(5_000.0, "Only consider observed trades with at least this notional (USDC).")
    max_entry_odds: float = _doc(0.35, "Only copy BUYs at/below these odds — insider EV lives at long odds; also auto-excludes market-makers and favorites.")
    max_wallet_age_days: float = _doc(14.0, "Wallet freshness window — older wallets aren't 'fresh'.")
    max_prior_trades: int = _doc(10, "Max prior trades for a wallet to count as fresh.")
    # Title matching is the weaker screen: " vs " also catches head-to-head
    # political markets, and it misses "Will <team> win on <date>?".
    exclude_title_patterns: tuple = _doc(_DEFAULT_EXCLUDES, "Skip markets whose title contains any of these (sports filter). Empty list disables.")
    # The authoritative sports screen — Gamma tags what titles miss.
    # "sports" also matches "esports" by substring.
    exclude_categories: tuple = _doc(("sports",), "Gamma category/tag substrings to reject (lowercased). Empty list disables.")

    # ── Time-value gate ──────────────────────────────────────────────────────
    # Capital locked in a far-future market has opportunity cost (~10%/yr in
    # an index fund) and insiders act on *imminent* events — every documented
    # case resolved within days. Skip markets resolving further out than
    # max_days_to_resolution, and require the win-case return, linearly
    # annualized over the time to resolution, to clear a hurdle: +5% resolving
    # tomorrow is a great trade, +5% locked for a year is strictly worse than
    # the S&P. Unknown end date fails closed.
    max_days_to_resolution: float = _doc(30.0, "Skip markets resolving further out than this many days.")
    min_annualized_return: float = _doc(1.0, "Win-case return, annualized over time-to-resolution, must beat this (1.0 = +100%/yr).")

    # ── Sizing + cadence ─────────────────────────────────────────────────────
    bet_size_usdc: float = _doc(2.0, "Our copy size — top-up target per market (USDC).")
    poll_interval_seconds: int = _doc(15, "Seconds between firehose polls.")
    firehose_limit: int = _doc(100, "Rows per /trades firehose page.")
    settle_check_every: int = _doc(20, "Polls between market-resolution sweeps.")

    # ── Candidate buffer — select the best signals, don't copy them all ─────
    # Passing candidates are held for this window, ranked by conviction
    # score, and only the top N are copied (the rest are real people being
    # randomly dumb, not insiders). 0 disables buffering (copy immediately).
    # Trade-off: waiting costs entry price on fast movers — the slippage
    # gate still rejects anything that drifted > max_slippage meanwhile.
    buffer_window_seconds: float = _doc(900.0, "Candidate buffer window (seconds); 0 = copy immediately.")
    buffer_top_n: int = _doc(2, "Copy only the N best-scored candidates per window.")
    buffer_max: int = _doc(20, "Safety valve — a burst filling the buffer flushes it early.")

    # ── Risk caps (the AlgoParams surface — this algo's pool only) ─────────
    max_position_size_usdc: float = _doc(2.0, "Max spend per market.")
    max_total_exposure_usdc: float = _doc(10.0, "Max total open exposure for this algorithm.")
    daily_loss_limit_usdc: float = _doc(5.0, "Suspend buys if realized P&L is down this much today.")
    min_order_size_usdc: float = _doc(1.0, "Skip orders smaller than this.")
    max_slippage: float = _doc(0.10, "Max drift from the signal price before we skip. Insider signals move fast, so a tight gate here is adverse selection against exactly the trades we want.")

    # ── Notifications ────────────────────────────────────────────────────────
    webhook_url: str = _doc(
        "https://discord.com/api/webhooks/1514867567519596594/tPzzQrqH5_0X5oXIV2iQnH5hFiSMe4h0iNyvQslN5xNHhSUn7lfPOB4_KRdLDKJbe99Q",
        "Discord webhook for this algorithm's trade alerts. Required — there is no global fallback.",
    )

    # ── Order placement / paper ──────────────────────────────────────────────
    order_type: str = _doc("market", "'market' (FOK/FAK) or 'limit' (GTC).")
    paper_starting_balance: float = _doc(20.0, "Virtual balance — seeded into the DB on FIRST run only.")
    paper_fee_bps: float = _doc(0.0, "Modeled paper fee, basis points.")

    def validate(self) -> None:
        """Boot-time sanity checks — called by the profile loader."""
        if not (0.0 < self.max_entry_odds <= 1.0):
            raise ValueError(f"max_entry_odds must be in (0, 1], got {self.max_entry_odds}.")
        if self.order_type not in ("market", "limit"):
            raise ValueError(f"order_type must be 'market' or 'limit', got '{self.order_type}'.")
        if self.min_cash_size_usdc <= 0:
            raise ValueError("min_cash_size_usdc must be positive.")
        if not self.webhook_url:
            raise ValueError(
                "webhook_url is required — there is no global fallback, so an "
                "algorithm without one would trade without ever notifying."
            )
