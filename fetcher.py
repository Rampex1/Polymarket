"""
Phase 1: Trade fetcher — discovers surfandturf's wallet and polls for new trades.
"""

import time
import logging
from typing import Callable, Optional

import requests

import config
from models import Trade

logger = logging.getLogger(__name__)

SESSION = requests.Session()
SESSION.headers.update({"Accept": "application/json"})


# ---------------------------------------------------------------------------
# Wallet lookup
# ---------------------------------------------------------------------------

_WALLET_LOOKUP_INSTRUCTIONS = """
Could not auto-resolve the wallet address for '{username}'.

Polymarket's profile API requires auth. To find the wallet manually:
  1. Go to https://polymarket.com and search for the user.
  2. Open their profile page — the URL will contain their proxy wallet address.
     Example: https://polymarket.com/profile/0xabc123...
  3. Copy that address and add it to your .env file:
     TARGET_ADDRESS=0xabc123...
  4. Re-run the bot.
"""


def lookup_wallet(username: str) -> Optional[str]:
    """
    Return the Polygon proxy-wallet address for a Polymarket username.
    Tries the Gamma API first; if that fails (requires auth), prints
    manual instructions and returns None.
    """
    url = f"{config.GAMMA_API}/profiles"
    try:
        resp = SESSION.get(url, params={"username": username}, timeout=10)
        if resp.status_code == 401:
            print(_WALLET_LOOKUP_INSTRUCTIONS.format(username=username))
            return None
        resp.raise_for_status()
        data = resp.json()
        for profile in data if isinstance(data, list) else [data]:
            if profile.get("name", "").lower() == username.lower():
                address = profile.get("proxyWallet") or profile.get("address")
                if address:
                    logger.info("Resolved %s → %s", username, address)
                    return address
    except requests.RequestException as e:
        logger.error("Wallet lookup failed: %s", e)
    print(_WALLET_LOOKUP_INSTRUCTIONS.format(username=username))
    return None


# ---------------------------------------------------------------------------
# Trade fetching
# ---------------------------------------------------------------------------

def fetch_recent_trades(address: str, limit: int = 100) -> list[Trade]:
    """Fetch the most recent trades for a wallet address."""
    url = f"{config.DATA_API}/activity"
    params = {"user": address, "limit": limit}
    try:
        resp = SESSION.get(url, params=params, timeout=10)
        resp.raise_for_status()
        raw = resp.json()
    except requests.RequestException as e:
        logger.error("Failed to fetch trades: %s", e)
        return []

    trades = []
    for item in raw if isinstance(raw, list) else raw.get("data", []):
        trade = _parse_trade(item)
        if trade is not None:
            trades.append(trade)
    return trades


def _parse_trade(item: dict) -> Optional[Trade]:
    """Parse a raw activity item into a Trade. Returns None if it should be skipped."""
    try:
        # Only TRADE type is actionable; skip REDEEM, MAKER_REBATE, REWARD, etc.
        if item.get("type") != "TRADE":
            return None

        size = float(item.get("usdcSize") or 0)
        if size < config.MIN_TRADE_SIZE_USDC:
            return None

        action = item.get("side", "").upper()  # "BUY" or "SELL"
        if action not in ("BUY", "SELL"):
            return None

        outcome = item.get("outcome", "")

        return Trade(
            id=item.get("transactionHash", ""),
            market_id=item.get("conditionId", ""),
            question=item.get("title", ""),
            side=action,
            size_usdc=size,
            price=float(item.get("price") or 0),
            action=action,
            timestamp=int(item.get("timestamp") or time.time()),
            outcome=outcome,
            asset_id=item.get("asset"),
        )
    except (TypeError, ValueError, KeyError) as e:
        logger.debug("Skipping unparseable trade item: %s — %s", item, e)
        return None


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------

def poll(address: str, on_trade: Callable[[Trade], None] = None) -> None:
    """Continuously poll for new trades and invoke on_trade for each one."""
    seen_ids: set[str] = set()

    # Seed with existing trades so we don't replay history on startup
    initial = fetch_recent_trades(address)
    for t in initial:
        seen_ids.add(t.id)
    logger.info("Seeded with %d existing trades. Watching for new ones...", len(seen_ids))

    while True:
        time.sleep(config.POLL_INTERVAL_SECONDS)
        trades = fetch_recent_trades(address)
        new_trades = [t for t in trades if t.id not in seen_ids]

        for trade in sorted(new_trades, key=lambda t: t.timestamp):
            seen_ids.add(trade.id)
            logger.info("New trade detected: %s", trade)
            if on_trade:
                on_trade(trade)

        if not new_trades:
            logger.debug("No new trades found.")
