"""
api.py

Polymarket HTTP reads — wallet lookup, trades, positions, prices, resolution.
All requests share one retrying session so transient 429/5xx don't drop trades.
"""

import json
import logging
import time
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .. import config
from ..domain.records import GlobalTrade, Trade

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
            # Min-size filtering is per-algorithm; the API layer emits every
            # parseable trade and the algorithm decides what to ignore.
            if size <= 0:
                return None
            action = item.get("side", "").upper()
            if action not in ("BUY", "SELL"):
                return None
            return Trade(
                id=tx_hash,
                market_id=condition_id,
                question=item.get("title", ""),
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
                size_usdc=float(item.get("usdcSize") or 0),
                price=0.0,
                action="REDEEM",
                timestamp=int(item.get("timestamp") or time.time()),
                outcome=item.get("outcome", ""),
                asset_id=item.get("asset") or None,
            )

        if trade_type == "MERGE":
            # A merge means the target redeemed complementary YES+NO shares
            # for $1/pair, fully exiting their directional bet on this market.
            # We mirror it as a full close of our matching position — the
            # executor doesn't try to compute a partial ratio from the merge
            # USDC (it's in $/pair units, not mark-to-market USD).
            tx_hash = item.get("transactionHash")
            condition_id = item.get("conditionId")
            if not (tx_hash and condition_id):
                return None
            return Trade(
                id=tx_hash,
                market_id=condition_id,
                question=item.get("title", ""),
                size_usdc=float(item.get("usdcSize") or 0),
                price=0.0,
                action="MERGE",
                timestamp=int(item.get("timestamp") or time.time()),
                outcome=item.get("outcome", ""),
                # asset_id may be absent — a merge spans both sides of the
                # market. The executor falls back to our position's asset_id.
                asset_id=item.get("asset") or None,
            )

        return None
    except (TypeError, ValueError, KeyError) as e:
        logger.debug("Skipping unparseable trade item: %s — %s", item, e)
        return None


def fetch_user_positions(address: str) -> list[dict]:
    """All open positions for `address`, as raw rows from the Data API.

    Used by copy_trade to size against the target wallet's holdings.
    Returns an empty list on failure — callers treat that as "couldn't check
    this tick" rather than "no positions".
    """
    try:
        resp = SESSION.get(
            f"{config.DATA_API}/positions",
            params={"user": address},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("data", [])
    except Exception as e:
        logger.warning("Could not fetch positions for %s: %s", address, e)
        return []


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
            # Gamma responds 200 with an unfiltered first page for an
            # unrecognised parameter spelling.  Never treat that arbitrary
            # market as this market's resolution — it can settle the wrong
            # token.  Try the fallback spelling instead.
            for row in rows:
                if str(row.get("conditionId") or "").lower() == market_id.lower():
                    return row
    except Exception as e:
        logger.debug("Gamma market lookup failed for %s: %s", market_id, e)
    return None


def _flag_true(val) -> bool:
    """Gamma boolean fields arrive as bools or strings depending on endpoint."""
    if isinstance(val, bool):
        return val
    return isinstance(val, str) and val.lower() in ("true", "1", "yes")


def market_is_resolved(market: dict) -> bool:
    """Check Gamma's closed/resolved flags. Either being truthy implies finality.

    Lives here (not in runner) so paper-mode algorithms can check resolution
    without transitively importing py_clob_client.
    """
    return any(_flag_true(market.get(key)) for key in ("closed", "resolved", "archived"))


def market_outcome_is_final(market: dict) -> bool:
    """True only when Gamma reports a *determined* outcome — stricter than
    `market_is_resolved`.

    `closed` alone is not finality: a market stops trading at its end date
    but can sit undetermined through the UMA proposal/dispute window, during
    which `outcomePrices` still mirrors the last order book. Anything that
    books P&L or writes outcome labels (which are write-once) must use this
    check, not the loose one.

    Final means an explicit resolution marker (`resolved` flag or
    `umaResolutionStatus == "resolved"`), or — belt and braces — a closed
    market whose outcomePrices vector is already pinned to exact 0/1, which
    Polymarket only writes at resolution.
    """
    if _flag_true(market.get("resolved")):
        return True
    uma = market.get("umaResolutionStatus")
    if isinstance(uma, str) and uma.lower() == "resolved":
        return True

    if not _flag_true(market.get("closed")):
        return False
    prices = market.get("outcomePrices")
    try:
        price_list = json.loads(prices) if isinstance(prices, str) else prices
        if not price_list:
            return False
        floats = [float(p) for p in price_list]
    except (TypeError, ValueError):
        return False
    # Exact-pin test: live books quote at most 0.999, so 0/1 means settled.
    return all(f in (0.0, 1.0) for f in floats) and 1.0 in floats


# ---------------------------------------------------------------------------
# Platform-wide discovery primitives
# ---------------------------------------------------------------------------
#
# These power account discovery (finding wallets worth copying) rather than
# following an already-known target. All of them follow the same failure
# contract as the rest of this module: log + return an empty/None sentinel,
# never raise to the caller.


def fetch_global_trades(min_cash_usdc: float, limit: int = 100) -> list[GlobalTrade]:
    """Platform-wide trades at or above a USDC notional (the whale firehose).

    Uses the Data API's server-side cash filter so we only pay bandwidth for
    trades big enough to matter. Returns newest-first rows; failure → [].
    """
    try:
        resp = SESSION.get(
            f"{config.DATA_API}/trades",
            params={
                "filterType": "CASH",
                "filterAmount": min_cash_usdc,
                "limit": limit,
            },
            timeout=10,
        )
        resp.raise_for_status()
        raw = resp.json()
    except requests.RequestException as e:
        logger.error("Failed to fetch global trades: %s", e)
        return []

    trades = []
    for item in raw if isinstance(raw, list) else raw.get("data", []):
        trade = _parse_global_trade(item)
        if trade is not None:
            trades.append(trade)
    return trades


def _parse_global_trade(item: dict) -> Optional[GlobalTrade]:
    """Parse a raw /trades row. Returns None when the row can't be acted on
    (no tx hash to dedupe, no market/asset to route, non-positive economics)."""
    try:
        tx_hash = item.get("transactionHash")
        condition_id = item.get("conditionId")
        wallet = item.get("proxyWallet")
        asset_id = item.get("asset")
        if not (tx_hash and condition_id and wallet and asset_id):
            return None

        price = float(item.get("price") or 0)
        shares = float(item.get("size") or 0)
        if price <= 0 or shares <= 0:
            return None

        return GlobalTrade(
            tx_hash=tx_hash,
            wallet=wallet,
            side=item.get("side", "").upper(),
            price=price,
            shares=shares,
            cash_usdc=shares * price,
            market_id=condition_id,
            asset_id=asset_id,
            timestamp=int(item.get("timestamp") or time.time()),
            title=item.get("title", ""),
            outcome=item.get("outcome", ""),
            trader_name=item.get("name") or item.get("pseudonym") or "",
        )
    except (TypeError, ValueError, KeyError) as e:
        logger.debug("Skipping unparseable global trade: %s — %s", item, e)
        return None


def fetch_wallet_stats(address: str, max_rows: int = 100) -> Optional[dict]:
    """Cheap freshness profile of a wallet from one /activity page.

    Returns {"trade_count", "activity_count", "oldest_ts", "capped"}.
    `capped=True` means the page came back full — the wallet has at least
    `max_rows` activities and is by definition not fresh, so callers can
    short-circuit without paginating. `oldest_ts` is None for a wallet with
    zero recorded activity (brand new, or the API hasn't propagated yet).

    Failure → None, so callers can fail CLOSED: a wallet we can't verify is
    a wallet we don't copy.
    """
    try:
        resp = SESSION.get(
            f"{config.DATA_API}/activity",
            params={"user": address, "limit": max_rows},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        rows = data if isinstance(data, list) else data.get("data", [])
    except Exception as e:
        logger.warning("Could not fetch wallet stats for %s: %s", address, e)
        return None

    # A null timestamp (row not yet propagated) must not become epoch-zero —
    # that would make a brand-new wallet look decades old.
    timestamps = [int(r["timestamp"]) for r in rows if r.get("timestamp")]
    return {
        "trade_count": sum(1 for r in rows if r.get("type") == "TRADE"),
        "activity_count": len(rows),
        "oldest_ts": min(timestamps) if timestamps else None,
        "capped": len(rows) >= max_rows,
    }


def fetch_wallet_value(address: str) -> Optional[float]:
    """Total USD value of a wallet's open positions (Data API /value).

    Enrichment for signal feature logging (enables offline Kelly inversion:
    bet_fraction ≈ cash / (value + cash)). Defensive about response shape;
    any failure or surprise → None. Never gates a signal.
    """
    try:
        resp = SESSION.get(
            f"{config.DATA_API}/value",
            params={"user": address},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            data = data[0] if data else {}
        if isinstance(data, dict) and data.get("value") is not None:
            return float(data["value"])
    except Exception as e:
        logger.warning("Could not fetch wallet value for %s: %s", address, e)
    return None


def fetch_price_history(
    token_id: str,
    fidelity: int = 60,
    interval: str = "max",
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
) -> Optional[list[dict]]:
    """Price timeseries for a token from the public CLOB endpoint.

    Returns the raw `history` list ([{t, p}, ...]). An empty list is a real
    answer — the CLOB drops history for long-resolved markets — while None
    means the HTTP call itself failed (caller may retry later).

    `startTs`/`endTs` and `interval` are mutually exclusive on the API;
    passing either timestamp drops the interval parameter.
    """
    params: dict = {"market": token_id, "fidelity": fidelity}
    if start_ts is not None or end_ts is not None:
        if start_ts is not None:
            params["startTs"] = start_ts
        if end_ts is not None:
            params["endTs"] = end_ts
    else:
        params["interval"] = interval

    try:
        resp = SESSION.get(
            f"{config.CLOB_API}/prices-history", params=params, timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        history = data.get("history", []) if isinstance(data, dict) else []
        return history if isinstance(history, list) else []
    except Exception as e:
        logger.warning("Could not fetch price history for %s: %s", token_id, e)
        return None


def fetch_top_markets(
    closed: bool,
    limit: int = 50,
    end_date_min: Optional[str] = None,
    end_date_max: Optional[str] = None,
) -> list[dict]:
    """Top markets by volume from Gamma (universe selection for discovery).

    Note: `order=volumeNum` — Gamma's `order=volume` sorts the *string* field
    and returns garbage. Booleans must be lowercase strings. Failure → [].
    """
    params: dict = {
        "closed": "true" if closed else "false",
        "order": "volumeNum",
        "ascending": "false",
        "limit": limit,
    }
    if end_date_min:
        params["end_date_min"] = end_date_min
    if end_date_max:
        params["end_date_max"] = end_date_max

    try:
        resp = SESSION.get(f"{config.GAMMA_API}/markets", params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("data", [])
    except Exception as e:
        logger.warning("Could not fetch top markets: %s", e)
        return []

