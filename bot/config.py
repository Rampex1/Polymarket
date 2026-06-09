"""
Global infrastructure config.

Scope rules
-----------
This file holds only settings that are *bot-wide infrastructure* — the same
for every algorithm running in the process:

  * Polymarket credentials and API base URLs (one wallet, one CLOB session).
  * Database path.
  * Discord webhook URL and timezone.

Mode (paper vs live) is **per-algorithm** now and lives in each algorithm's
`params.py`. There is no global PAPER_TRADE toggle.

Algorithm-specific settings (tiers, sizing, risk caps, poll cadence,
slippage tolerance, target wallet, etc.) live with the algorithm in
`algorithms/<algo_name>/params.py`.

Profiles
--------
A "profile" is the bundle of algorithms a given process runs. Pick one at
startup via the `PROFILE` env var (default: `default`). This file looks for
`.env.<profile>` first and falls back to `.env`, so each profile can have
its own credentials, DB path, Discord channel, etc.

    PROFILE=prod          python main.py     # loads .env.prod
    PROFILE=experimental  python main.py     # loads .env.experimental
"""

import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


# ── Profile selection (must run before any os.getenv reads below) ────────────

PROFILE: str = os.getenv("PROFILE", "default")

# Load .env.<profile> if it exists, otherwise fall back to plain .env.
# `override=True` so a stale shell env doesn't shadow file values.
for _candidate in (f".env.{PROFILE}", ".env"):
    if os.path.exists(_candidate):
        load_dotenv(_candidate, override=True)
        break


# ── Polymarket API base URLs ─────────────────────────────────────────────────

GAMMA_API: str = "https://gamma-api.polymarket.com"
DATA_API:  str = "https://data-api.polymarket.com"
CLOB_API:  str = "https://clob.polymarket.com"


# ── Polymarket credentials (live mode only; ignored in paper) ────────────────
POLY_PRIVATE_KEY:     str = os.getenv("POLY_PRIVATE_KEY", "")
POLY_API_KEY:         str = os.getenv("POLY_API_KEY", "")
POLY_API_SECRET:      str = os.getenv("POLY_API_SECRET", "")
POLY_API_PASSPHRASE:  str = os.getenv("POLY_API_PASSPHRASE", "")
POLY_FUNDER_ADDRESS:  str = os.getenv("POLY_FUNDER_ADDRESS", "")


# ── Storage ──────────────────────────────────────────────────────────────────

DB_PATH: str = os.getenv("DB_PATH", "positions.db")


# ── Notifications ────────────────────────────────────────────────────────────

DISCORD_WEBHOOK_URL: str = os.getenv("DISCORD_WEBHOOK_URL", "")

# Timezone for daily-summary rollovers. Defaults to UTC so cadence is
# deterministic regardless of where the VPS is hosted.
_TZ_NAME = os.getenv("TIMEZONE", "UTC")
try:
    TIMEZONE = ZoneInfo(_TZ_NAME)
except ZoneInfoNotFoundError:
    TIMEZONE = ZoneInfo("UTC")
