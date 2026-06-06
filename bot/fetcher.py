"""
Trade fetcher — discovers the target wallet and polls for new trades.

Design notes:
  * `SESSION` is a shared requests.Session() with a retry-backoff adapter so
    transient 429/5xx errors don't silently drop trades.
  * `seen_ids` in `poll()` is bounded to avoid an unbounded memory leak —
    we only need to remember the *recent* trades to deduplicate against.
  * `fetch_target_position_value` accepts an `expected_min` hint so callers
    can defeat the Data API's eventual-consistency race (the freshly-seen
    BUY may not yet be reflected when we ask about the wallet's holdings).
"""

import collections
import logging
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
        backoff_factor=0.5,         # 0.5s, 1s, 2s between retries
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
            # Defensive: reject rows missing fields we *must* have to act on the
            # signal. A row with no transactionHash can't be deduped; a row
            # with no conditionId or asset can't be routed to an order.
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

    `expected_min` defeats the Data API's eventual consistency: we just saw a
    BUY on this market, so the wallet's holding *cannot* be smaller than that
    trade size. If the API hasn't caught up yet, retry briefly until it has.
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
    """
    Returns the last traded price for a token via the public CLOB endpoint.
    A resolved winning token trades at ~1.0; a losing token at ~0.0.
    Returns None if the price cannot be determined.
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
    """Best-effort canonical resolution status from the Gamma API.

    Used as a fallback when the CLOB last-trade-price is ambiguous (between
    0.1 and 0.9). Returns a dict with at least `closed`/`resolved` flags when
    the market is finalized; returns None on lookup failure.
    """
    try:
        resp = SESSION.get(
            f"{config.GAMMA_API}/markets",
            params={"condition_ids": market_id},
            timeout=8,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        rows = data if isinstance(data, list) else [data]
        return rows[0] if rows else None
    except Exception as e:
        logger.debug("Gamma market lookup failed for %s: %s", market_id, e)
        return None


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------

# Cap on the dedupe ring — we only need enough history that we don't replay
# trades. 5000 is ~weeks of activity for an active trader; the actual memory
# footprint is tiny but it keeps the set from growing unbounded.
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
    # Bounded LRU set — old trade IDs fall off the back automatically.
    seen_ids: collections.OrderedDict[str, None] = collections.OrderedDict()

    def _mark_seen(trade_id: str) -> None:
        if trade_id in seen_ids:
            seen_ids.move_to_end(trade_id)
        else:
            seen_ids[trade_id] = None
            if len(seen_ids) > SEEN_IDS_MAX:
                seen_ids.popitem(last=False)

    # Seed with existing trades so we don't replay history on startup.
    for t in fetch_recent_trades(address):
        _mark_seen(t.id)
    logger.info(
        "Seeded with %d existing trades. Watching for new ones...", len(seen_ids),
    )

    while True:
        # Use an Event-based wait when available so shutdown is instant.
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
                    # Surface but don't crash the poll loop — one bad trade
                    # shouldn't take down the bot.
                    logger.exception("on_trade handler raised for %s", trade.id)

        if not new_trades:
            logger.debug("No new trades found.")
