"""
Trade fetcher — discovers the target wallet and polls for new trades.

Design notes:
  * `SESSION` is a shared requests.Session() with a retry-backoff adapter so
    transient 429/5xx errors don't silently drop trades.
  * `seen_ids` in `poll()` is bounded so the long-running process doesn't
    leak memory.
  * `fetch_target_position_value` accepts an `expected_min` hint so callers
    can defeat the Data API's eventual-consistency race.
  * A small in-memory cache (`target_holding_cache`) tracks the target's
    last observed holding per market. The SELL path relies on it to compute
    an accurate close ratio without trusting the volatile combination of
    "post-sell value reported by the API" and "USDC notional of the sell".
"""

import collections
import logging
import threading
import time
from typing import Callable, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import config
from .models import Trade

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTTP session with retries
# ---------------------------------------------------------------------------

def _build_session() -> requests.Session:
    """Session with retry/backoff so transient errors don't drop trade signals."""
    session = requests.Session()
    session.headers.update({"Accept": "application/json"})
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


SESSION = _build_session()


# ---------------------------------------------------------------------------
# Target-holding cache
# ---------------------------------------------------------------------------
#
# Why this exists
# ---------------
# The SELL path needs to know what fraction of their position the target is
# closing, so we can mirror it proportionally. Reconstructing "holding before
# sell" from "holding after sell" + "USD notional of sell" is unsafe:
#   (a) the Data API is eventually consistent — "after" may still show the
#       pre-sell value, so adding the notional double-counts;
#   (b) holding values are at *current* price, while sell notional is at the
#       *fill* price, so they can't be added cleanly when the price drifts.
#
# Instead we cache the last observed holding per market. The BUY path
# populates the cache (we already fetch it for tier sizing). On SELL, the
# cached value IS the pre-sell holding — no reconstruction needed. The cache
# is then decremented in place. Cache miss → safe fallback (full close).


class TargetHoldingCache:
    """Thread-safe in-memory cache of the target's last-observed holding."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, float] = {}

    def set(self, market_id: str, value: float) -> None:
        with self._lock:
            self._data[market_id] = max(0.0, float(value))

    def get(self, market_id: str) -> Optional[float]:
        with self._lock:
            return self._data.get(market_id)

    def decrement(self, market_id: str, amount: float) -> None:
        with self._lock:
            if market_id in self._data:
                self._data[market_id] = max(0.0, self._data[market_id] - float(amount))

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


target_holding_cache = TargetHoldingCache()


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
    """Return the Polygon proxy-wallet address for a Polymarket username."""
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
        trade_type = item.get("type")

        if trade_type == "TRADE":
            # Reject rows missing fields we *must* have to act on the signal.
            # No tx hash → can't dedupe; no conditionId/asset → can't route.
            tx_hash = item.get("transactionHash")
            condition_id = item.get("conditionId")
            asset_id = item.get("asset")
            if not (tx_hash and condition_id and asset_id):
                return None

            size = float(item.get("usdcSize") or 0)
            if size <= 0 or size < config.MIN_TRADE_SIZE_USDC:
                return None
            action = item.get("side", "").upper()
            if action not in ("BUY", "SELL"):
                return None
            return Trade(
                id=tx_hash,
                market_id=condition_id,
                question=item.get("title", ""),
                side=action,
                size_usdc=size,
                price=float(item.get("price") or 0),
                action=action,
                timestamp=int(item.get("timestamp") or time.time()),
                outcome=item.get("outcome", ""),
                asset_id=asset_id,
            )

        if trade_type == "REDEEM":
            return Trade(
                id=item.get("transactionHash", ""),
                market_id=item.get("conditionId", ""),
                question=item.get("title", ""),
                side="REDEEM",
                size_usdc=float(item.get("usdcSize") or 0),
                price=0.0,
                action="REDEEM",
                timestamp=int(item.get("timestamp") or time.time()),
                outcome=item.get("outcome", ""),
                asset_id=item.get("asset") or None,
            )

        return None
    except (TypeError, ValueError, KeyError) as e:
        logger.debug("Skipping unparseable trade item: %s — %s", item, e)
        return None


def fetch_target_position_value(
    address: str,
    market_id: str,
    expected_min: float = 0.0,
    retries: int = 3,
    retry_wait: float = 0.5,
) -> float:
    """Total USDC value of the target's current position in a market.

    `expected_min` defeats the Data API's eventual consistency: when we know
    the holding *must* be at least N USD (e.g. the BUY we just observed),
    we retry until the API agrees.
    """
    last_value = 0.0
    for attempt in range(retries):
        try:
            resp = SESSION.get(
                f"{config.DATA_API}/positions",
                params={"user": address, "market": market_id},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            rows = data if isinstance(data, list) else data.get("data", [])
            total = sum(float(r.get("value") or r.get("currentValue") or 0) for r in rows)
            last_value = total
            if total >= expected_min or expected_min <= 0:
                return total
        except Exception as e:
            logger.warning(
                "Could not fetch position for %s in %s (attempt %d): %s",
                address, market_id, attempt + 1, e,
            )
        time.sleep(retry_wait)
    return last_value


def fetch_resolution_price(asset_id: str) -> Optional[float]:
    """Last traded price for a token via the public CLOB endpoint.

    Used as a fallback resolution signal — a winning token trades at ~1.0,
    a losing token at ~0.0. Returns None if unavailable.
    """
    try:
        resp = SESSION.get(
            f"{config.CLOB_API}/last-trade-price",
            params={"token_id": asset_id},
            timeout=8,
        )
        if resp.status_code == 200:
            price = float(resp.json().get("price", -1))
            if price >= 0:
                return price
    except Exception as e:
        logger.warning("Could not fetch resolution price for %s: %s", asset_id, e)
    return None


def fetch_market_resolution(market_id: str) -> Optional[dict]:
    """Canonical resolution snapshot from Gamma.

    Returns the raw market dict (with `closed`/`resolved` flags and
    `outcomePrices`) so callers can decide whether the market is *actually*
    settled before booking P&L. Returns None on lookup failure.
    """
    try:
        # Try both the plural and singular forms — Gamma's parameter naming
        # has varied across versions; whichever matches will return the row.
        for param in ("condition_ids", "condition_id"):
            resp = SESSION.get(
                f"{config.GAMMA_API}/markets",
                params={param: market_id},
                timeout=8,
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
            rows = data if isinstance(data, list) else [data]
            if rows:
                return rows[0]
    except Exception as e:
        logger.debug("Gamma market lookup failed for %s: %s", market_id, e)
    return None


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------

# Bound on the dedupe ring. Sized for ~weeks of an active trader's history;
# tune up if monitoring a high-frequency target.
SEEN_IDS_MAX = 5000


def poll(
    address: str,
    on_trade: Callable[[Trade], None] = None,
    stop_event=None,
) -> None:
    """Continuously poll for new trades and invoke on_trade for each one.

    `stop_event` (a threading.Event) lets main.py cleanly interrupt the
    sleep instead of relying on signal-driven sys.exit, which can land
    in the middle of an order placement.
    """
    seen_ids: collections.OrderedDict[str, None] = collections.OrderedDict()

    def _mark_seen(trade_id: str) -> None:
        if trade_id in seen_ids:
            seen_ids.move_to_end(trade_id)
        else:
            seen_ids[trade_id] = None
            if len(seen_ids) > SEEN_IDS_MAX:
                seen_ids.popitem(last=False)

    for t in fetch_recent_trades(address):
        _mark_seen(t.id)
    logger.info(
        "Seeded with %d existing trades. Watching for new ones...", len(seen_ids),
    )

    while True:
        if stop_event is not None:
            if stop_event.wait(config.POLL_INTERVAL_SECONDS):
                logger.info("Poll loop received stop signal, exiting.")
                return
        else:
            time.sleep(config.POLL_INTERVAL_SECONDS)

        trades = fetch_recent_trades(address)
        new_trades = [t for t in trades if t.id and t.id not in seen_ids]

        for trade in sorted(new_trades, key=lambda t: t.timestamp):
            _mark_seen(trade.id)
            logger.info("New trade detected: %s", trade)
            if on_trade:
                try:
                    on_trade(trade)
                except Exception:
                    # One bad trade must not kill the loop.
                    logger.exception("on_trade handler raised for %s", trade.id)

        if not new_trades:
            logger.debug("No new trades found.")
