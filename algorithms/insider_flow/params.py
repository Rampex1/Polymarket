"""
Configuration for the insider-flow algorithm.

Env naming: every knob is `INSIDERFLOW_<NAME>`. Unlike copy_trade there is
no legacy fallback — this algorithm is new.

Every field reads its env var through `default_factory` so the environment
is re-evaluated per instantiation (matters for tests, and means a profile
can construct params before/after dotenv loading without import-order traps).
"""

import os
from dataclasses import dataclass, field

from bot.algorithm import Mode
from bot.config import env_value

_P = "INSIDERFLOW_"


def _env(name: str, default: str) -> str:
    return env_value(_P + name, default=default)


def _default_mode() -> Mode:
    return Mode(_env("MODE", "paper").lower())


def _default_exclude_titles() -> tuple:
    """Comma-separated env override; default screens out the sports firehose.

    Caveat: " vs " also matches head-to-head political markets ("Trump vs.
    Newsom…"). Acceptable for v1 — sports dominates the big-cash feed by an
    order of magnitude. Clear INSIDERFLOW_EXCLUDE_TITLES to disable.
    """
    raw = os.getenv(_P + "EXCLUDE_TITLES")
    if raw is not None and raw != "":
        return tuple(p for p in raw.split(",") if p)
    return (" vs. ", " vs ", "O/U", "Spread")


@dataclass(frozen=True)
class InsiderFlowParams:
    name: str = "insider_flow"
    mode: Mode = field(default_factory=_default_mode)

    # ── Signal filters ───────────────────────────────────────────────────────
    # Only copy BUYs ≥ this notional…
    min_cash_size_usdc: float = field(
        default_factory=lambda: float(_env("MIN_CASH", "5000")))
    # …at long odds (insider EV lives below ~0.35; also auto-excludes
    # market-makers and favorites bought at 0.9+)…
    max_entry_odds: float = field(
        default_factory=lambda: float(_env("MAX_ODDS", "0.35")))
    # …from wallets younger than this / with fewer prior trades than this.
    max_wallet_age_days: float = field(
        default_factory=lambda: float(_env("MAX_WALLET_AGE_DAYS", "14")))
    max_prior_trades: int = field(
        default_factory=lambda: int(_env("MAX_PRIOR_TRADES", "10")))
    exclude_title_patterns: tuple = field(default_factory=_default_exclude_titles)

    # ── Sizing + cadence ─────────────────────────────────────────────────────
    bet_size_usdc: float = field(
        default_factory=lambda: float(_env("BET_SIZE", "10")))
    poll_interval_seconds: int = field(
        default_factory=lambda: int(_env("POLL_INTERVAL", "15")))
    firehose_limit: int = field(
        default_factory=lambda: int(_env("FIREHOSE_LIMIT", "100")))
    # Open positions are exited at resolution; sweep every N polls.
    settle_check_every: int = field(
        default_factory=lambda: int(_env("SETTLE_EVERY", "20")))

    # ── Risk caps (AlgoParams protocol — this algo's pool only) ─────────────
    max_position_size_usdc: float = field(
        default_factory=lambda: float(_env("MAX_POSITION", "10")))
    max_total_exposure_usdc: float = field(
        default_factory=lambda: float(_env("MAX_EXPOSURE", "100")))
    daily_loss_limit_usdc: float = field(
        default_factory=lambda: float(_env("DAILY_LOSS_LIMIT", "50")))
    min_order_size_usdc: float = field(
        default_factory=lambda: float(_env("MIN_ORDER", "1")))
    # Wider than copy_trade's 0.05 — these signals move fast and skipping on
    # drift is adverse selection against exactly the trades we want.
    max_slippage: float = field(
        default_factory=lambda: float(_env("MAX_SLIPPAGE", "0.10")))

    # ── Order placement / paper ──────────────────────────────────────────────
    order_type: str = field(
        default_factory=lambda: _env("ORDER_TYPE", "market").lower())
    paper_starting_balance: float = field(
        default_factory=lambda: float(_env("PAPER_BALANCE", "10000")))
    paper_fee_bps: float = field(
        default_factory=lambda: float(_env("PAPER_FEE_BPS", "0")))


PARAMS = InsiderFlowParams()
