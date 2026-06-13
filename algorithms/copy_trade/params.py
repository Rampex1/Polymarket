"""
Copy-trade parameter schema.

This file defines *what knobs exist* — names, types, defaults, and docs.
The values an actual deployment runs with live in `config/<profile>.toml`;
this module reads no environment variables. `python -m bot.params
copy_trade` prints the full knob list from this schema.
"""

from dataclasses import dataclass, field

from bot.algorithm import Mode


def _doc(default, doc: str):
    return field(default=default, metadata={"doc": doc})


@dataclass(frozen=True)
class CopyTradeParams:
    name: str = "copy_trade"
    mode: Mode = Mode.PAPER          # fail-safe default; profiles set explicitly

    # ── Signal source ────────────────────────────────────────────────────────
    target_address: str = _doc("", "Target's proxy wallet (from their Polymarket profile URL). Preferred over username.")
    target_username: str = _doc("", "Target's username — resolved to a wallet via Gamma /profiles at startup.")

    # ── Polling ──────────────────────────────────────────────────────────────
    poll_interval_seconds: int = _doc(20, "Seconds between polls of the target's activity feed.")
    min_trade_size_usdc: float = _doc(0.0, "Ignore target trades smaller than this notional. 0 = no filter.")
    settle_check_every: int = _doc(45, "Polls between resolution sweeps of our open positions — safety net for target REDEEMs missed while the bot was offline.")

    # ── Tier sizing — total target position size for our bet ─────────────────
    # Holding < tier1_min          → skip
    # [tier1_min, tier1_max]       → tier1_size
    # (tier1_max, tier2_max]       → tier2_size
    # (tier2_max, +∞)              → tier3_size
    tier1_min: float = _doc(80_000.0, "Target holding below this → skip entirely (the high-conviction floor).")
    tier1_max: float = _doc(150_000.0, "Upper bound of tier 1.")
    tier1_size: float = _doc(1.0, "Our total position size (USDC) for tier-1 holdings.")
    tier2_max: float = _doc(300_000.0, "Upper bound of tier 2.")
    tier2_size: float = _doc(2.0, "Our total position size (USDC) for tier-2 holdings.")
    tier3_size: float = _doc(3.0, "Our total position size (USDC) above tier2_max.")

    # ── Risk caps (this algorithm's pool only — independent of others) ──────
    max_position_size_usdc: float = _doc(3.0, "Max spend per market.")
    max_total_exposure_usdc: float = _doc(12.0, "Max total open exposure for this algorithm.")
    daily_loss_limit_usdc: float = _doc(4.0, "Suspend buys if realized P&L is down this much today.")
    min_order_size_usdc: float = _doc(1.0, "Skip top-ups smaller than this.")
    max_slippage: float = _doc(0.05, "Skip order if price moved more than this fraction from the signal.")

    # ── Order placement ──────────────────────────────────────────────────────
    order_type: str = _doc("market", "'market' (FOK/FAK) or 'limit' (GTC).")

    # ── Paper mode ───────────────────────────────────────────────────────────
    paper_starting_balance: float = _doc(10_000.0, "Virtual balance — seeded into the DB on FIRST run only; later edits need scripts/reset_paper_trade_db.py or a manual UPDATE.")
    paper_fee_bps: float = _doc(0.0, "Modeled paper fee, basis points.")

    def validate(self) -> None:
        """Boot-time sanity checks — called by the profile loader."""
        if not (self.target_address or self.target_username):
            raise ValueError(
                "copy_trade needs a target — set target_address or "
                "target_username under [algorithm.params]."
            )
        if self.order_type not in ("market", "limit"):
            raise ValueError(f"order_type must be 'market' or 'limit', got '{self.order_type}'.")
        if not (0 < self.tier1_min <= self.tier1_max <= self.tier2_max):
            raise ValueError("tiers must satisfy 0 < tier1_min <= tier1_max <= tier2_max.")
