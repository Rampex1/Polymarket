import os
import tomllib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


PROFILE: str = os.getenv("PROFILE", "")

if os.path.exists(".env"):
    load_dotenv(".env", override=True)


# ── Polymarket API base URLs ─────────────────────────────────────────────────

GAMMA_API: str = "https://gamma-api.polymarket.com"
DATA_API: str = "https://data-api.polymarket.com"
CLOB_API: str = "https://clob.polymarket.com"


# ── Polymarket credentials (live mode only; ignored in paper) ────────────────
POLY_PRIVATE_KEY: str = os.getenv("POLY_PRIVATE_KEY", "")
POLY_API_KEY: str = os.getenv("POLY_API_KEY", "")
POLY_API_SECRET: str = os.getenv("POLY_API_SECRET", "")
POLY_API_PASSPHRASE: str = os.getenv("POLY_API_PASSPHRASE", "")
POLY_FUNDER_ADDRESS: str = os.getenv("POLY_FUNDER_ADDRESS", "")

# _SIGNATURE_TYPE 3 is used for new accounta
POLY_SIGNATURE_TYPE: int = int(os.getenv("POLY_SIGNATURE_TYPE", "3"))


# ── Storage ──────────────────────────────────────────────────────────────────

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _default_db_path() -> str:
    """Positions DB path — DB_PATH if set, else <repo>/data/positions.db."""
    return os.getenv("DB_PATH") or os.path.join(REPO_ROOT, "data", "positions.db")


DB_PATH: str = _default_db_path()


# ── Discord bot (optional two-way interface) ─────────────────────────────────

DISCORD_BOT_TOKEN: str = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_GUILD_ID: str = os.getenv("DISCORD_GUILD_ID", "")


# ── Notifications ────────────────────────────────────────────────────────────

_WEBHOOK_REGISTRY = os.path.join("config", "webhooks.toml")


def resolve_summary_webhook(
    profile: str, registry_path: str = _WEBHOOK_REGISTRY
) -> str:
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


HEARTBEAT_INTERVAL_HOURS: float = float(os.getenv("HEARTBEAT_INTERVAL_HOURS", "6"))

_TZ_NAME = os.getenv("TIMEZONE", "America/Los_Angeles")
try:
    TIMEZONE = ZoneInfo(_TZ_NAME)
except ZoneInfoNotFoundError:
    TIMEZONE = ZoneInfo("America/Los_Angeles")
