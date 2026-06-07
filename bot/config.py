"""
Global infrastructure config.

Scope rules
-----------
This file holds only settings that are *bot-wide infrastructure* — the same
for every algorithm running in the process:

  * Polymarket credentials and API base URLs (one wallet, one CLOB session).
  * The single paper-vs-live mode flag.
  * Database path.
  * Telegram credentials and timezone.

Algorithm-specific settings (tiers, sizing, risk caps, poll cadence,
starting paper balance, slippage tolerance, target wallet, etc.) live with
the algorithm in `algorithms/<algo_name>/params.py`. Don't add new
algorithm knobs here.
"""

import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

load_dotenv()


# ── Polymarket API base URLs ─────────────────────────────────────────────────

GAMMA_API: str = "https://gamma-api.polymarket.com"
DATA_API:  str = "https://data-api.polymarket.com"
CLOB_API:  str = "https://clob.polymarket.com"


# ── Execution mode (single, process-wide) ────────────────────────────────────
# Set to True to log orders without sending them to the exchange. Affects
# *all* algorithms uniformly — there's no "one algo paper, another live"
# mode because they share the same CLOB client and wallet.
PAPER_TRADE: bool = os.getenv("PAPER_TRADE", "true").lower() != "false"


# ── Polymarket credentials (live mode only; ignored in paper) ────────────────
POLY_PRIVATE_KEY:     str = os.getenv("POLY_PRIVATE_KEY", "")
POLY_API_KEY:         str = os.getenv("POLY_API_KEY", "")
POLY_API_SECRET:      str = os.getenv("POLY_API_SECRET", "")
POLY_API_PASSPHRASE:  str = os.getenv("POLY_API_PASSPHRASE", "")
POLY_FUNDER_ADDRESS:  str = os.getenv("POLY_FUNDER_ADDRESS", "")


# ── Storage ──────────────────────────────────────────────────────────────────

DB_PATH: str = os.getenv("DB_PATH", "positions.db")


# ── Notifications ────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID:   str = os.getenv("TELEGRAM_CHAT_ID", "")

# Timezone for daily-summary rollovers. Defaults to UTC so cadence is
# deterministic regardless of where the VPS is hosted.
_TZ_NAME = os.getenv("TIMEZONE", "UTC")
try:
    TIMEZONE = ZoneInfo(_TZ_NAME)
except ZoneInfoNotFoundError:
    TIMEZONE = ZoneInfo("UTC")
