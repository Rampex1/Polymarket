"""Single read-side gateway for Polymarket data.

The existing ``bot.fetcher`` functions remain the transport implementation
and public compatibility surface.  Strategies use this object instead, which
makes their external dependency explicit and easy to replace in tests.
"""

from typing import Optional

from ... import fetcher
from ...domain.models import GlobalTrade, Trade


class MarketDataGateway:
    """Read-only access to activity, market metadata, and CLOB prices."""

    def lookup_wallet(self, username: str) -> Optional[str]:
        return fetcher.lookup_wallet(username)

    def recent_trades(self, address: str, limit: int = 100) -> list[Trade]:
        return fetcher.fetch_recent_trades(address, limit)

    def user_positions(self, address: str) -> list[dict]:
        return fetcher.fetch_user_positions(address)

    def target_position_value(
        self, address: str, market_id: str, expected_min: float = 0.0,
    ) -> Optional[float]:
        return fetcher.fetch_target_position_value(address, market_id, expected_min)

    def global_trades(self, min_cash_usdc: float, limit: int = 100) -> list[GlobalTrade]:
        return fetcher.fetch_global_trades(min_cash_usdc, limit)

    def market(self, market_id: str) -> Optional[dict]:
        return fetcher.fetch_market_resolution(market_id)

    def price(self, asset_id: str) -> Optional[float]:
        return fetcher.fetch_resolution_price(asset_id)

    def wallet_stats(self, address: str, max_rows: int = 100) -> Optional[dict]:
        return fetcher.fetch_wallet_stats(address, max_rows)

    def wallet_value(self, address: str) -> Optional[float]:
        return fetcher.fetch_wallet_value(address)

    @staticmethod
    def market_outcome_is_final(market: dict) -> bool:
        return fetcher.market_outcome_is_final(market)


DEFAULT_MARKET_DATA = MarketDataGateway()
