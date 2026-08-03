"""Price lookup and slippage policy for execution."""

import logging
from typing import Optional

from py_clob_client_v2.client import ClobClient

from .. import fetcher
from ..domain.models import Trade

logger = logging.getLogger(__name__)


def current_price(trade: Trade, client: Optional[ClobClient]) -> Optional[float]:
    """Return a current asset price, failing closed at the caller."""
    if client is not None:
        try:
            return float(client.get_last_trade_price(trade.asset_id).get("price"))
        except Exception as exc:
            logger.warning("Authenticated price fetch failed: %s", exc)
    return fetcher.fetch_resolution_price(trade.asset_id)


def slippage_ok(
    trade: Trade, current_price: Optional[float], max_slippage: float, algo_name: str,
) -> bool:
    """Reject unavailable prices and excessive drift from the signal price."""
    if current_price is None:
        logger.warning("[%s] No current price for slippage check — refusing: %s",
                       algo_name, trade.question[:50])
        return False
    if trade.price <= 0:
        return True
    drift = abs(current_price - trade.price) / trade.price
    if drift > max_slippage:
        logger.warning("[%s] Slippage %.1f%% > max %.1f%%, skipping: %s",
                       algo_name, drift * 100, max_slippage * 100,
                       trade.question[:50])
        return False
    return True
