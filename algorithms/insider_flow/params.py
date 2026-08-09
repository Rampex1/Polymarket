"""
Insider-flow parameter schema.

Pure schema — names, types, docs. No defaults: every profile block states
every knob.
"""

from dataclasses import dataclass, field

from bot.domain.intents import Mode


def _doc(doc: str):
    return field(metadata={"doc": doc})


@dataclass(frozen=True)
class InsiderFlowParams:
    name: str
    mode: Mode

    # ── Signal filters ───────────────────────────────────────────────────────
    min_cash_size_usdc: float = _doc("Only consider observed trades with at least this notional (USDC).")
    max_entry_odds: float = _doc("Only copy BUYs at/below these odds — insider EV lives at long odds; also auto-excludes market-makers and favorites.")
    max_wallet_age_days: float = _doc("Wallet freshness window — older wallets aren't 'fresh'.")
    max_prior_trades: int = _doc("Max prior trades for a wallet to count as fresh.")
    # Title matching is the weaker screen: " vs " also catches head-to-head
    # political markets, and it misses "Will <team> win on <date>?".
    exclude_title_patterns: tuple = _doc("Skip markets whose title contains any of these (sports filter). Empty list disables.")
    # The authoritative sports screen — Gamma tags what titles miss.
    # "sports" also matches "esports" by substring.
    exclude_categories: tuple = _doc("Gamma category/tag substrings to reject (lowercased). Empty list disables.")

    # ── Time-value gate ──────────────────────────────────────────────────────
    # Capital locked in a far-future market has opportunity cost (~10%/yr in
    # an index fund) and insiders act on *imminent* events — every documented
    # case resolved within days. Skip markets resolving further out than
    # max_days_to_resolution, and require the win-case return, linearly
    # annualized over the time to resolution, to clear a hurdle: +5% resolving
    # tomorrow is a great trade, +5% locked for a year is strictly worse than
    # the S&P. Unknown end date fails closed.
    max_days_to_resolution: float = _doc("Skip markets resolving further out than this many days.")
    min_annualized_return: float = _doc("Win-case return, annualized over time-to-resolution, must beat this (1.0 = +100%/yr).")

    # ── Sizing + cadence ─────────────────────────────────────────────────────
    bet_size_usdc: float = _doc("Our copy size — top-up target per market (USDC).")
    poll_interval_seconds: int = _doc("Seconds between firehose polls.")
    firehose_limit: int = _doc("Rows per /trades firehose page.")
    settle_check_every: int = _doc("Polls between market-resolution sweeps.")

    # ── Candidate buffer — select the best signals, don't copy them all ─────
    # Passing candidates are held for this window, ranked by conviction
    # score, and only the top N are copied (the rest are real people being
    # randomly dumb, not insiders). 0 disables buffering (copy immediately).
    # Trade-off: waiting costs entry price on fast movers — the slippage
    # gate still rejects anything that drifted > max_slippage meanwhile.
    buffer_window_seconds: float = _doc("Candidate buffer window (seconds); 0 = copy immediately.")
    buffer_top_n: int = _doc("Copy only the N best-scored candidates per window.")
    buffer_max: int = _doc("Safety valve — a burst filling the buffer flushes it early.")

    # ── Risk caps (AlgoParams protocol — this algo's pool only) ─────────────
    max_position_size_usdc: float = _doc("Max spend per market.")
    max_total_exposure_usdc: float = _doc("Max total open exposure for this algorithm.")
    daily_loss_limit_usdc: float = _doc("Suspend buys if realized P&L is down this much today.")
    min_order_size_usdc: float = _doc("Skip orders smaller than this.")
    max_slippage: float = _doc("Max drift from the signal price before we skip. Insider signals move fast, so a tight gate here is adverse selection against exactly the trades we want.")

    # ── Notifications ────────────────────────────────────────────────────────
    webhook_url: str = _doc("Per-algorithm Discord webhook URL. Overrides the global registry; '' falls back to config/webhooks.toml routing.")

    # ── Order placement / paper ──────────────────────────────────────────────
    order_type: str = _doc("'market' (FOK/FAK) or 'limit' (GTC).")
    paper_starting_balance: float = _doc("Virtual balance — seeded into the DB on FIRST run only.")
    paper_fee_bps: float = _doc("Modeled paper fee, basis points.")

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
