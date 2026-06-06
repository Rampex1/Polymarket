"""
Centralized configuration loaded from environment variables.

All values are read at import time. Treat this as immutable after startup —
if you need to change a setting mid-run, restart the process so all modules
pick up the new value consistently.
"""

import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

load_dotenv()


# ── Target to copy ───────────────────────────────────────────────────────────

# TARGET_USERNAME is read from env. TARGET_ADDRESS wins if set (skips
# the username lookup). Default empty so we fail loudly with a missing-
# config error instead of silently following a hardcoded handle.
TARGET_USERNAME: str = os.getenv("TARGET_USERNAME", "")
TARGET_ADDRESS: str = os.getenv("TARGET_ADDRESS", "")

# ── Polling ──────────────────────────────────────────────────────────────────

POLL_INTERVAL_SECONDS: int = int(os.getenv("POLL_INTERVAL_SECONDS", "20"))

# Skip target trades smaller than this. Default 0.0 keeps prior behavior
# (no filter); raise to ignore dust signals.
MIN_TRADE_SIZE_USDC: float = float(os.getenv("MIN_TRADE_SIZE_USDC", "0.0"))

# ── Polymarket API base URLs ─────────────────────────────────────────────────

GAMMA_API: str = "https://gamma-api.polymarket.com"
DATA_API: str = "https://data-api.polymarket.com"
CLOB_API: str = "https://clob.polymarket.com"

# ── Execution ────────────────────────────────────────────────────────────────

# Set to True to log orders without sending them to the exchange.
PAPER_TRADE: bool = os.getenv("PAPER_TRADE", "true").lower() != "false"

# Starting virtual balance for paper trading.
PAPER_STARTING_BALANCE: float = float(os.getenv("PAPER_STARTING_BALANCE", "10000.0"))

# Tiered bet sizing based on target's total position value in a market.
# Holding < TIER1_MIN → skip the trade.
#   [TIER1_MIN, TIER1_MAX]  → TIER1_SIZE  (default $80k–$150k → $1)
#   (TIER1_MAX, TIER2_MAX]  → TIER2_SIZE  (default $150k–$300k → $2)
#   (TIER2_MAX, +∞)         → TIER3_SIZE  (default $300k+ → $3)
TIER1_MIN:  float = float(os.getenv("TIER1_MIN",  "80000"))
TIER1_MAX:  float = float(os.getenv("TIER1_MAX",  "150000"))
TIER1_SIZE: float = float(os.getenv("TIER1_SIZE", "1.0"))
TIER2_MAX:  float = float(os.getenv("TIER2_MAX",  "300000"))
TIER2_SIZE: float = float(os.getenv("TIER2_SIZE", "2.0"))
TIER3_SIZE: float = float(os.getenv("TIER3_SIZE", "3.0"))

# Order type: "market" (FOK) or "limit" (GTC at signal price).
ORDER_TYPE: str = os.getenv("ORDER_TYPE", "market")

# Per-order floor — skip if scaled size is below this. Polymarket has a
# protocol-level $1 minimum, so any non-zero scaled bet under $1 will be
# rejected on chain; enforce it here instead of paying a failed-order penalty.
MIN_ORDER_SIZE_USDC: float = float(os.getenv("MIN_ORDER_SIZE_USDC", "1.0"))

# Max allowed price movement since signal (fraction, e.g. 0.05 = 5%).
MAX_SLIPPAGE: float = float(os.getenv("MAX_SLIPPAGE", "0.05"))

# Flat per-side trade fee assumed for paper P&L modeling. Polymarket charges
# real fees on live fills; modeling them in paper avoids systematically
# overstating profitability vs. what live will produce.
PAPER_FEE_BPS: float = float(os.getenv("PAPER_FEE_BPS", "0"))

# Your Polymarket credentials (EOA private key + CLOB API keys).
POLY_PRIVATE_KEY: str = os.getenv("POLY_PRIVATE_KEY", "")
POLY_API_KEY: str = os.getenv("POLY_API_KEY", "")
POLY_API_SECRET: str = os.getenv("POLY_API_SECRET", "")
POLY_API_PASSPHRASE: str = os.getenv("POLY_API_PASSPHRASE", "")
# Your proxy wallet address (shown in Polymarket profile URL).
POLY_FUNDER_ADDRESS: str = os.getenv("POLY_FUNDER_ADDRESS", "")

# ── Risk limits ──────────────────────────────────────────────────────────────

# NOTE: defaults are deliberately *small* so the bot fails safe if a user runs
# without an .env. Raise these in .env once you've verified behavior.
MAX_POSITION_SIZE_USDC: float = float(os.getenv("MAX_POSITION_SIZE_USDC", "2.0"))
MAX_TOTAL_EXPOSURE_USDC: float = float(os.getenv("MAX_TOTAL_EXPOSURE_USDC", "12.0"))
DAILY_LOSS_LIMIT_USDC: float = float(os.getenv("DAILY_LOSS_LIMIT_USDC", "4.0"))

# ── Notifications ────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

# Timezone for the midnight daily-summary roll. Defaults to UTC so the
# rollover is deterministic regardless of where the VPS is hosted.
_TZ_NAME = os.getenv("TIMEZONE", "UTC")
try:
    TIMEZONE = ZoneInfo(_TZ_NAME)
except ZoneInfoNotFoundError:
    TIMEZONE = ZoneInfo("UTC")

# ── Storage ──────────────────────────────────────────────────────────────────

DB_PATH: str = os.getenv("DB_PATH", "positions.db")
