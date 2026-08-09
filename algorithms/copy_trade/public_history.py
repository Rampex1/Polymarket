"""Public-API ingestion for the ranked copy-trade history store.

This is intentionally an offline job.  The strategy worker reads only the
normalized SQLite rows, so a slow public endpoint can never delay copying a
leader trade.

The Data API provides public BUY fills for a wallet.  We retain a fill only
after Gamma confirms the market has a final outcome, then label its token
using the final outcome prices.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
import logging
from typing import Any

from bot.polymarket import api
from bot.execution.settlement import gamma_outcome_price

from .ranker import ResolvedBet

logger = logging.getLogger(__name__)

_LEADERBOARD_REQUESTS = (
    {"category": "OVERALL", "timePeriod": "ALL", "orderBy": "PNL", "limit": 50},
    {"category": "OVERALL", "timePeriod": "ALL", "orderBy": "VOL", "limit": 50},
    {"category": "OVERALL", "timePeriod": "MONTH", "orderBy": "PNL", "limit": 50},
)


def leaderboard_wallets(session: Any = api.SESSION) -> list[str]:
    """Return a deduplicated, public candidate pool from three leaderboards."""
    wallets: list[str] = []
    seen: set[str] = set()
    for params in _LEADERBOARD_REQUESTS:
        try:
            response = session.get(
                f"{api.config.DATA_API}/v1/leaderboard", params=params, timeout=10,
            )
            response.raise_for_status()
            rows = response.json()
        except Exception as exc:
            logger.warning("Could not fetch Polymarket leaderboard: %s", exc)
            continue
        for row in rows if isinstance(rows, list) else []:
            wallet = str(row.get("proxyWallet") or "").lower()
            if wallet and wallet not in seen:
                seen.add(wallet)
                wallets.append(wallet)
    return wallets


def wallet_buys(
    wallet: str, max_pages: int, history_end: int | None = None,
    session: Any = api.SESSION,
) -> list[dict]:
    """Fetch up to ``max_pages`` of public BUY fills for one wallet."""
    rows: list[dict] = []
    for page in range(max_pages):
        try:
            params = {
                "user": wallet,
                "side": "BUY",
                "takerOnly": "false",
                # A positive start requests the full user-scoped history
                # rather than the endpoint's rolling default window.
                "start": 1,
                "limit": 500,
                "offset": page * 500,
            }
            if history_end is not None:
                params["end"] = history_end
            response = session.get(
                f"{api.config.DATA_API}/trades",
                params=params,
                timeout=10,
            )
            response.raise_for_status()
            batch = response.json()
        except Exception as exc:
            logger.warning("Could not fetch BUY history for %s: %s", wallet, exc)
            break
        batch = batch if isinstance(batch, list) else batch.get("data", [])
        if not batch:
            break
        rows.extend(row for row in batch if isinstance(row, dict))
        if len(batch) < 500:
            break
    return rows


def closed_positions(wallet: str, max_pages: int, session: Any = api.SESSION) -> list[dict]:
    """Fetch closed positions for the conservative paper-history fallback."""
    rows: list[dict] = []
    for page in range(max_pages):
        try:
            response = session.get(
                f"{api.config.DATA_API}/closed-positions",
                params={"user": wallet, "limit": 50, "offset": page * 50,
                        "sortBy": "TIMESTAMP", "sortDirection": "DESC"},
                timeout=10,
            )
            response.raise_for_status()
            batch = response.json()
        except Exception as exc:
            logger.warning("Could not fetch closed positions for %s: %s", wallet, exc)
            break
        batch = batch if isinstance(batch, list) else batch.get("data", [])
        if not batch:
            break
        rows.extend(row for row in batch if isinstance(row, dict))
        if len(batch) < 50:
            break
    return rows


def _timestamp(value: object) -> int | None:
    """Parse epoch seconds or Gamma's ISO timestamp into Unix seconds."""
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(float(value))
    except ValueError:
        pass
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def resolved_bet_from_trade(wallet: str, trade: dict, market: dict | None) -> ResolvedBet | None:
    """Normalize one resolved BUY fill, failing closed on ambiguity."""
    if not market or not api.market_outcome_is_final(market):
        return None
    asset_id = trade.get("asset")
    try:
        entry_price = float(trade["price"])
    except (KeyError, TypeError, ValueError):
        return None
    outcome = gamma_outcome_price(market, asset_id)
    if not (asset_id and 0 < entry_price < 1 and outcome in (0.0, 1.0)):
        return None
    # Gamma's closedTime is the closest available canonical resolution time.
    # The trade timestamp is only a conservative fallback for older rows.
    resolved_at = _timestamp(market.get("closedTime") or market.get("updatedAt"))
    resolved_at = resolved_at or _timestamp(trade.get("timestamp"))
    if resolved_at is None:
        return None
    return ResolvedBet(wallet.lower(), entry_price, outcome, resolved_at)


def resolved_bet_from_closed_position(
    wallet: str, position: dict, minimum_age_seconds: int, now: int,
) -> ResolvedBet | None:
    """Conservative public fallback when archived Gamma market rows are absent.

    This is suitable only for paper ranking: a position is retained only if
    its public current price is exactly binary and its scheduled end date is
    well past. Unlike the Gamma path, it cannot prove the final outcome.
    """
    try:
        entry_price = float(position["avgPrice"])
        outcome = float(position["curPrice"])
    except (KeyError, TypeError, ValueError):
        return None
    end_at = _timestamp(position.get("endDate"))
    if not (0 < entry_price < 1 and outcome in (0.0, 1.0) and end_at
            and end_at <= now - minimum_age_seconds):
        return None
    return ResolvedBet(wallet.lower(), entry_price, outcome,
                       _timestamp(position.get("timestamp")) or end_at)


def collect_closed_position_bets(
    wallets: Iterable[str], max_pages_per_wallet: int, minimum_age_seconds: int,
    now: int, positions_for_wallet: Callable[[str, int], list[dict]] = closed_positions,
) -> list[ResolvedBet]:
    """Collect conservative binary closed-position labels for paper testing."""
    bets: list[ResolvedBet] = []
    for wallet in wallets:
        for position in positions_for_wallet(wallet, max_pages_per_wallet):
            bet = resolved_bet_from_closed_position(wallet, position, minimum_age_seconds, now)
            if bet is not None:
                bets.append(bet)
    return bets


def collect_resolved_bets(
    wallets: Iterable[str],
    max_pages_per_wallet: int,
    trades_for_wallet: Callable[[str, int, int | None], list[dict]] = wallet_buys,
    market_for_id: Callable[[str], dict | None] = api.fetch_market_resolution,
    history_end: int | None = None,
) -> list[ResolvedBet]:
    """Collect normalized, final-outcome rows for a bounded wallet list."""
    market_cache: dict[str, dict | None] = {}
    bets: list[ResolvedBet] = []
    for wallet in wallets:
        for trade in trades_for_wallet(wallet, max_pages_per_wallet, history_end):
            market_id = trade.get("conditionId")
            if not market_id:
                continue
            if market_id not in market_cache:
                market_cache[market_id] = market_for_id(str(market_id))
            bet = resolved_bet_from_trade(wallet, trade, market_cache[market_id])
            if bet is not None:
                bets.append(bet)
    return bets
