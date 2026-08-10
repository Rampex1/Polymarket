"""Canonical resolution-price selection.

Settlement is kept separate from active order execution because it never
places an order and follows stricter market-finality rules.
"""

import json
import logging
from typing import Iterator, Optional

from ..domain.intents import SettleIntent
from ..polymarket import DEFAULT_MARKET_DATA

logger = logging.getLogger(__name__)


def sweep_resolved(
    ledger, market_data, paper: bool, algo_name: str, poll_count: int,
) -> Iterator[SettleIntent]:
    """Yield a SettleIntent for every open position whose outcome is settled.

    Strict gate: `closed` alone means trading ended, not that the outcome is
    determined (UMA dispute window). Settling there would book P&L off a
    stale book and permanently mislabel the signal rows, which are
    write-once. Undetermined markets wait for a later sweep — resolution is
    not time-sensitive. Gamma lagging on its `resolved` flag is already
    covered: `market_outcome_is_final` accepts a closed market whose
    outcomePrices are pinned to exact 0/1.
    """
    for pos in ledger.all_open(paper=paper):
        market = market_data.market(pos.market_id)
        if not (market and market_data.market_outcome_is_final(market)):
            continue
        logger.info("[%s] Market resolved — settling: %s", algo_name, pos.question[:55])
        yield SettleIntent(
            market_id=pos.market_id,
            question=pos.question,
            outcome=pos.outcome,
            signal_id=f"settle:{pos.market_id}:{poll_count}",
            reason="market resolved (sweep)",
        )


def resolve_close_price(
    market_id: str, asset_id: str, market_data=None,
) -> Optional[float]:
    """Final Gamma outcome data, else a binary CLOB price on a closed market.

    Returning None is always safe: the caller leaves the position open and a
    later sweep retries. Settling early is not — P&L would be booked off a
    live book, the on-chain position would drift from the DB, and the
    signal's outcome label is write-once.
    """
    md = market_data or DEFAULT_MARKET_DATA
    market = md.market(market_id)
    if market and md.market_outcome_is_final(market):
        outcome_price = gamma_outcome_price(market, asset_id)
        if outcome_price is not None:
            return outcome_price

    # CLOB fallback — for markets Gamma hasn't caught up on yet. Only valid
    # once trading has STOPPED: a live favourite sits at 0.97 for weeks
    # without being decided, and `fetch_resolution_price` is the last trade
    # on the open book, not a resolution feed.
    if not (market and md.market_is_resolved(market)):
        return None

    price = md.price(asset_id)
    if price is None:
        return None
    if price > 0.95:
        return 1.0
    if price < 0.05:
        return 0.0
    return None


def gamma_outcome_price(market: dict, asset_id: str) -> Optional[float]:
    """Extract the resolved price for one CLOB token from Gamma payload."""
    tokens, prices = market.get("clobTokenIds"), market.get("outcomePrices")
    if not (tokens and prices):
        return None
    try:
        token_list = json.loads(tokens) if isinstance(tokens, str) else tokens
        price_list = json.loads(prices) if isinstance(prices, str) else prices
        for token, price in zip(token_list, price_list):
            if token == asset_id:
                return float(price)
    except (ValueError, TypeError) as exc:
        logger.debug("Gamma resolution parse failed: %s", exc)
    return None
