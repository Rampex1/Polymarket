"""Canonical resolution-price selection.

Settlement is kept separate from active order execution because it never
places an order and follows stricter market-finality rules.
"""

import json
import logging
from typing import Optional

from ..polymarket import api

logger = logging.getLogger(__name__)


def resolve_close_price(market_id: str, asset_id: str) -> Optional[float]:
    """Use final Gamma outcome data, then a confidently binary CLOB price."""
    market = api.fetch_market_resolution(market_id)
    if market and api.market_outcome_is_final(market):
        outcome_price = gamma_outcome_price(market, asset_id)
        if outcome_price is not None:
            return outcome_price

    price = api.fetch_resolution_price(asset_id)
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
