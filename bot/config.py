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


# ── Shared env helper (used by per-algorithm params modules) ─────────────────

def env_value(*names: str, default: str = "") -> str:
    """First non-empty value among environment variables `names`, else default.

    Treats "" the same as unset so an empty assignment in a .env file
    doesn't shadow a legacy fallback name.
    """
    for name in names:
        val = os.getenv(name)
        if val is not None and val != "":
            return val
    return default


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

# CLOB signature_type. Polymarket's CTF Exchange V2 added a new sig type
# for smart-contract wallets. Which one depends on how the account was
# created:
#   0 = Plain EOA (no proxy) — rare for Polymarket use.
#   1 = POLY_PROXY — legacy email/social signup accounts. Magic-link EOA
#       exported from Settings → Export private key.
#   2 = POLY_GNOSIS_SAFE — older browser-wallet signups (pre-2025) with
#       a Polymarket-deployed Gnosis Safe owned by the EOA.
#   3 = POLY_1271 — current smart-contract wallet accounts created via
#       the wallet-signup flow. Uses EIP-1271 signatures.
# Default 3 is the common case for accounts created in 2025+.
POLY_SIGNATURE_TYPE: int = int(os.getenv("POLY_SIGNATURE_TYPE", "3"))


# ── Storage ──────────────────────────────────────────────────────────────────

def _default_db_path() -> str:
    """Resolve the positions DB path. New default lives under data/.

    Legacy fallback: an existing ./positions.db with no data/ counterpart
    keeps being used (with a warning) so a deployment that pulls this change
    and restarts doesn't silently start trading against a fresh DB.
    """
    env = os.getenv("DB_PATH")
    if env:
        return env
    new_path = os.path.join("data", "positions.db")
    if os.path.exists("positions.db") and not os.path.exists(new_path):
        import logging
        logging.getLogger(__name__).warning(
            "Using legacy ./positions.db — move it to data/positions.db "
            "(or set DB_PATH) to adopt the new layout."
        )
        return "positions.db"
    return new_path


DB_PATH: str = _default_db_path()


# ── Notifications ────────────────────────────────────────────────────────────

DISCORD_WEBHOOK_URL: str = os.getenv("DISCORD_WEBHOOK_URL", "")

# Timezone for daily-summary rollovers. Defaults to US Pacific so the
# rollover lands at midnight PT regardless of where the VPS is hosted.
_TZ_NAME = os.getenv("TIMEZONE", "America/Los_Angeles")
try:
    TIMEZONE = ZoneInfo(_TZ_NAME)
except ZoneInfoNotFoundError:
    TIMEZONE = ZoneInfo("America/Los_Angeles")
