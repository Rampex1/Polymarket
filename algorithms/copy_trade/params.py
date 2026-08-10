"""
Copy-trade parameter schema.

This file defines *what knobs exist* — names, types, and docs. No defaults:
every profile block states every knob, and this module reads no environment
variables.
"""

from dataclasses import dataclass, field

from bot.domain.mode import Mode


def _doc(doc: str):
    return field(metadata={"doc": doc})


@dataclass(frozen=True)
class CopyTradeParams:
    name: str
    mode: Mode

    # ── Signal source ────────────────────────────────────────────────────────
    target_address: str = _doc("Target's proxy wallet (from their Polymarket profile URL). Preferred over username.")
    target_username: str = _doc("Target's username — resolved to a wallet via Gamma /profiles at startup.")

    # ── Ranked multi-leader mode ────────────────────────────────────────────
    # ``watchlist_size=0`` preserves the original single-target behavior.
    # A positive value activates the scorer-backed watchlist instead of the
    # static target fields above.
    watchlist_size: int = _doc("Top ranked wallets to watch; 0 keeps legacy single-target mode.")
    watchlist_candidate_wallets: tuple = _doc("Candidate proxy-wallet addresses supplied to the offline/history source.")
    watchlist_refresh_seconds: int = _doc("How often to rescore and atomically replace the active wallet cohort.")
    watchlist_min_resolved_bets: int = _doc("Minimum resolved bets before a wallet is eligible.")
    watchlist_confidence_z: float = _doc("One-sided confidence multiplier used for the edge lower bound.")
    watchlist_min_copyability_score: float = _doc("Reject wallets below this historical copyability score (0..1).")
    consensus_window_seconds: int = _doc("Time window in which distinct leader entries form a consensus.")
    consensus_min_leaders: int = _doc("Distinct active leaders required for consensus sizing.")
    consensus_size_multiplier: float = _doc("Multiplier applied to a tier target after consensus, capped by max position size.")
    max_concurrent_positions: int = _doc("Hard cap on simultaneously open markets for ranked multi-leader mode.")

    # ── Polling ──────────────────────────────────────────────────────────────
    poll_interval_seconds: int = _doc("Seconds between polls of the target's activity feed.")
    min_trade_size_usdc: float = _doc("Ignore target trades smaller than this notional. 0 = no filter.")
    settle_check_every: int = _doc("Polls between resolution sweeps of our open positions — safety net for target REDEEMs missed while the bot was offline.")

    # ── Tier sizing — total target position size for our bet ─────────────────
    # Holding < tier1_min          → skip
    # [tier1_min, tier1_max]       → tier1_size
    # (tier1_max, tier2_max]       → tier2_size
    # (tier2_max, +∞)              → tier3_size
    tier1_min: float = _doc("Target holding below this → skip entirely (the high-conviction floor).")
    tier1_max: float = _doc("Upper bound of tier 1.")
    tier1_size: float = _doc("Our total position size (USDC) for tier-1 holdings.")
    tier2_max: float = _doc("Upper bound of tier 2.")
    tier2_size: float = _doc("Our total position size (USDC) for tier-2 holdings.")
    tier3_size: float = _doc("Our total position size (USDC) above tier2_max.")

    # ── Risk caps (this algorithm's pool only — independent of others) ──────
    max_position_size_usdc: float = _doc("Max spend per market.")
    max_total_exposure_usdc: float = _doc("Max total open exposure for this algorithm.")
    daily_loss_limit_usdc: float = _doc("Suspend buys if realized P&L is down this much today.")
    min_order_size_usdc: float = _doc("Skip top-ups smaller than this.")
    max_slippage: float = _doc("Skip order if price moved more than this fraction from the signal.")

    # ── Order placement ──────────────────────────────────────────────────────
    order_type: str = _doc("'market' (FOK/FAK) or 'limit' (GTC).")

    # ── Notifications ────────────────────────────────────────────────────────
    webhook_url: str = _doc("Per-algorithm Discord webhook URL. Overrides the global registry; '' falls back to config/webhooks.toml routing.")

    # ── Paper mode ───────────────────────────────────────────────────────────
    paper_starting_balance: float = _doc("Virtual balance — seeded into the DB on FIRST run only; later edits need scripts/reset_paper_trade_db.py or a manual UPDATE.")
    paper_fee_bps: float = _doc("Modeled paper fee, basis points.")

    def validate(self) -> None:
        """Boot-time sanity checks — called by the profile loader."""
        if self.watchlist_size < 0:
            raise ValueError("watchlist_size must be zero or positive.")
        if self.watchlist_size == 0 and not (self.target_address or self.target_username):
            raise ValueError(
                "copy_trade needs a target, or set watchlist_size > 0 for "
                "ranked multi-leader mode."
            )
        if self.order_type not in ("market", "limit"):
            raise ValueError(f"order_type must be 'market' or 'limit', got '{self.order_type}'.")
        if not (0 < self.tier1_min <= self.tier1_max <= self.tier2_max):
            raise ValueError("tiers must satisfy 0 < tier1_min <= tier1_max <= tier2_max.")
        if self.watchlist_size > 0 and not self.watchlist_candidate_wallets:
            raise ValueError("ranked multi-leader mode needs watchlist_candidate_wallets.")
        if self.watchlist_refresh_seconds <= 0 or self.consensus_window_seconds <= 0:
            raise ValueError("watchlist_refresh_seconds and consensus_window_seconds must be positive.")
        if self.consensus_min_leaders < 2 or self.consensus_size_multiplier < 1:
            raise ValueError("consensus requires at least two leaders and a multiplier of at least one.")
        if self.max_concurrent_positions <= 0:
            raise ValueError("max_concurrent_positions must be positive.")
        if not self.webhook_url:
            raise ValueError(
                "webhook_url is required — there is no global fallback, so an "
                "algorithm without one would trade without ever notifying."
            )
