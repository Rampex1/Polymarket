"""
Global infrastructure config.

Scope rules
-----------
This file holds only settings that are *bot-wide infrastructure* — the same
for every algorithm running in the process:

  * Polymarket credentials and API base URLs (one wallet, one CLOB session).
  * Database path.
  * Discord webhook URL and timezone.

Mode (paper vs live) is **per-algorithm** and declared per block in
`config/<profile>.toml`. There is no global PAPER_TRADE toggle.

Algorithm-specific settings (tiers, sizing, risk caps, poll cadence,
slippage tolerance, target wallet, etc.) are NOT env-driven — they live
in `config/<profile>.toml`, validated against the schemas in
`algorithms/<algo_name>/params.py` (see bot/profile_loader.py).

Profiles
--------
A "profile" is the bundle of algorithms a given process runs. Pick one at
startup via the `PROFILE` env var — there is deliberately no default: the
bot refuses to boot without an explicit profile, so paper config can never
be confused with live.

    PROFILE=prod          python main.py
    PROFILE=experimental  python main.py

Secrets come from a single `.env` regardless of profile. Per-profile env
files are deliberately unsupported: paper's inability to touch real money
is enforced by the `allow_live` gate in the profile TOML, not by which
credentials happen to be on disk.
"""

import os
import tomllib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


# ── Profile selection (must run before any os.getenv reads below) ────────────

# No default on purpose — enforcement (refusing to boot) lives where the
# profile TOML is resolved (algorithms.ENABLED), so schema-only tooling
# like `python -m bot.params copy_trade` still works without a profile.
PROFILE: str = os.getenv("PROFILE", "")

# One .env for every profile. `override=True` so a stale shell env doesn't
# shadow file values.
if os.path.exists(".env"):
    load_dotenv(".env", override=True)


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


# ── Discord bot (optional two-way interface) ─────────────────────────────────

# Bot token from discord.com/developers → Application → Bot → Reset Token.
# Without this the bot is disabled; webhooks still work for one-way alerts.
DISCORD_BOT_TOKEN: str = os.getenv("DISCORD_BOT_TOKEN", "")

# Guild (server) ID for instant slash-command registration. Without it,
# commands are registered globally and can take up to 1 hour to appear.
# Enable Developer Mode in Discord → right-click server → Copy Server ID.
DISCORD_GUILD_ID: str = os.getenv("DISCORD_GUILD_ID", "")


# ── Notifications ────────────────────────────────────────────────────────────

_WEBHOOK_REGISTRY = os.path.join("config", "webhooks.toml")


def resolve_webhook(profile: str, registry_path: str = _WEBHOOK_REGISTRY) -> str:
    """Webhook for this profile: env override first, then the committed
    registry (config/webhooks.toml blocks route via their `profiles` list).

    The env var is an escape hatch, not the normal path — keeping webhooks
    in the registry makes channel changes git-pull-deployable.
    """
    env = os.getenv("DISCORD_WEBHOOK_URL", "")
    if env:
        return env
    try:
        with open(registry_path, "rb") as f:
            registry = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    for block in registry.values():
        if isinstance(block, dict) and profile and profile in block.get("profiles", []):
            return str(block.get("url", ""))
    return ""


DISCORD_WEBHOOK_URL: str = resolve_webhook(PROFILE)


def resolve_summary_webhook(profile: str, registry_path: str = _WEBHOOK_REGISTRY) -> str:
    """Summary webhook for this profile — the channel that receives the
    profile-level daily summary (all algos + combined total).

    Looks for a block in webhooks.toml with type = "summary" and
    profile = <name>. Returns "" if not configured (summary is skipped).
    """
    try:
        with open(registry_path, "rb") as f:
            registry = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    for block in registry.values():
        if (
            isinstance(block, dict)
            and block.get("type") == "summary"
            and profile
            and block.get("profile") == profile
        ):
            return str(block.get("url", ""))
    return ""

# How often the heartbeat ping fires. Set HEARTBEAT_INTERVAL_HOURS=0 to disable.
HEARTBEAT_INTERVAL_HOURS: float = float(os.getenv("HEARTBEAT_INTERVAL_HOURS", "6"))

# Timezone for daily-summary rollovers. Defaults to US Pacific so the
# rollover lands at midnight PT regardless of where the VPS is hosted.
_TZ_NAME = os.getenv("TIMEZONE", "America/Los_Angeles")
try:
    TIMEZONE = ZoneInfo(_TZ_NAME)
except ZoneInfoNotFoundError:
    TIMEZONE = ZoneInfo("America/Los_Angeles")
