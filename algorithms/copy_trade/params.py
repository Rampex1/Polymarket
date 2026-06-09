"""
Configuration for the copy-trade algorithm.

All knobs live in this file. Defaults match the historical behavior of the
pre-migration single-algo bot; env vars override at runtime.

Env naming convention
---------------------
New canonical names are prefixed `COPYTRADE_*`. Legacy unprefixed names
(e.g. `TARGET_ADDRESS`, `TIER1_SIZE`) are still accepted as a fallback so
existing `.env` files keep working unchanged. Drop the fallback once
deployments have migrated.
"""

import os
from dataclasses import dataclass, field

from bot.algorithm import Mode

_P = "COPYTRADE_"


def _env(new_name: str, legacy_name: str, default: str) -> str:
    """Return COPYTRADE_<NAME>, then <LEGACY_NAME>, then the default."""
    val = os.getenv(_P + new_name)
    if val is not None and val != "":
        return val
    val = os.getenv(legacy_name)
    if val is not None and val != "":
        return val
    return default


def _default_mode() -> Mode:
    """Pick the default mode.

    Order of precedence:
      1. COPYTRADE_MODE         — explicit per-algo setting.
      2. PAPER_TRADE            — legacy global toggle (still respected as a
                                  fallback so existing .env files boot).
      3. paper                  — fail-safe default.
    """
    explicit = os.getenv(_P + "MODE")
    if explicit:
        return Mode(explicit.lower())
    legacy = os.getenv("PAPER_TRADE")
    if legacy is not None:
        return Mode.PAPER if legacy.lower() != "false" else Mode.LIVE
    return Mode.PAPER


@dataclass(frozen=True)
class CopyTradeParams:
    name: str = "copy_trade"
    # default_factory so each instantiation re-reads env (relevant for tests
    # that set COPYTRADE_MODE per-case).
    mode: Mode = field(default_factory=_default_mode)

    # ── Signal source ────────────────────────────────────────────────────────
    target_address: str = _env("TARGET_ADDRESS", "TARGET_ADDRESS", "")
    target_username: str = _env("TARGET_USERNAME", "TARGET_USERNAME", "")

    # ── Polling ──────────────────────────────────────────────────────────────
    poll_interval_seconds: int = int(_env("POLL_INTERVAL", "POLL_INTERVAL_SECONDS", "20"))
    # Skip target trades smaller than this. 0 = no filter (legacy default).
    min_trade_size_usdc: float = float(_env("MIN_TRADE_SIZE", "MIN_TRADE_SIZE_USDC", "0.0"))

    # ── Tier sizing — total target position size for our bet ─────────────────
    # Holding < TIER1_MIN          → skip
    # [TIER1_MIN, TIER1_MAX]       → TIER1_SIZE
    # (TIER1_MAX, TIER2_MAX]       → TIER2_SIZE
    # (TIER2_MAX, +∞)              → TIER3_SIZE
    tier1_min:  float = float(_env("TIER1_MIN", "TIER1_MIN", "80000"))
    tier1_max:  float = float(_env("TIER1_MAX", "TIER1_MAX", "150000"))
    tier1_size: float = float(_env("TIER1_SIZE", "TIER1_SIZE", "1.0"))
    tier2_max:  float = float(_env("TIER2_MAX", "TIER2_MAX", "300000"))
    tier2_size: float = float(_env("TIER2_SIZE", "TIER2_SIZE", "2.0"))
    tier3_size: float = float(_env("TIER3_SIZE", "TIER3_SIZE", "3.0"))

    # ── Risk caps (this algorithm's pool only — independent of others) ──────
    max_position_size_usdc:  float = float(_env("MAX_POSITION", "MAX_POSITION_SIZE_USDC", "3.0"))
    max_total_exposure_usdc: float = float(_env("MAX_EXPOSURE", "MAX_TOTAL_EXPOSURE_USDC", "12.0"))
    daily_loss_limit_usdc:   float = float(_env("DAILY_LOSS_LIMIT", "DAILY_LOSS_LIMIT_USDC", "4.0"))
    min_order_size_usdc:     float = float(_env("MIN_ORDER", "MIN_ORDER_SIZE_USDC", "1.0"))
    max_slippage:            float = float(_env("MAX_SLIPPAGE", "MAX_SLIPPAGE", "0.05"))

    # ── Order placement ──────────────────────────────────────────────────────
    order_type: str = _env("ORDER_TYPE", "ORDER_TYPE", "market").lower()

    # ── Paper mode ───────────────────────────────────────────────────────────
    paper_starting_balance: float = float(_env("PAPER_BALANCE", "PAPER_STARTING_BALANCE", "10000.0"))
    paper_fee_bps: float = float(_env("PAPER_FEE_BPS", "PAPER_FEE_BPS", "0"))


PARAMS = CopyTradeParams()
